package cockroach

import (
	"context"
	// "database/sql"
	"time"

	// TODO: issue with below link: https://githubhelp.com/cockroachdb/cockroach-go/issues/65?ref=https://githubhelp.com
	// "github.com/cockroachdb/cockroach-go/crdb/crdbpgx"
	"github.com/cockroachdb/cockroach-go/v2/crdb/crdbpgx"
	"github.com/jackc/pgx/v4"
	// "github.com/cockroachdb/cockroach-go/crdb"
	"github.com/coreos/go-semver/semver"
	"github.com/interuss/dss/pkg/cockroach"
	"github.com/interuss/dss/pkg/logging"
	"github.com/interuss/dss/pkg/rid/repos"
	"github.com/interuss/stacktrace"
	"github.com/jonboulle/clockwork"
	"go.uber.org/zap"
)

const (
	// currentMajorSchemaVersion is the current major schema version.
	currentMajorSchemaVersion = 4

	//  Records expire if current time is <expiredDurationInMin> minutes more than records' endTime.
	expiredDurationInMin = 30
)

var (
	// DefaultClock is what is used as the Store's clock, returned from Dial.
	DefaultClock = clockwork.NewRealClock()
	// DefaultTimeout is the timeout applied to the txn retrier.
	// Note that this is not applied everywhere, but only
	// on the txn retrier.
	// If a given deadline is already supplied on the context, the earlier
	// deadline is used
	// TODO: use this in other function calls
	DefaultTimeout = 10 * time.Second

	// DatabaseName is the name of database storing remote ID data.
	DatabaseName = "rid"

	v400 = *semver.New("4.0.0")
)

type repo struct {
	repos.ISA
	repos.Subscription
}

// Store is an implementation of store.Store using Cockroach DB as its backend
// store.
//
// TODO: Add the SCD interfaces here, and collapse this store with the
// outer pkg/cockroach
type Store struct {
	db      *cockroach.DB
	logger  *zap.Logger
	clock   clockwork.Clock
	version *semver.Version
}

// NewStore returns a Store instance connected to a cockroach instance via db.
func NewStore(ctx context.Context, db *cockroach.DB, logger *zap.Logger) (*Store, error) {
	vs, err := db.GetVersion(ctx, DatabaseName)
	if err != nil {
		return nil, stacktrace.Propagate(err, "Failed to get database schema version for remote ID for db : %s", DatabaseName)
	}

	store := &Store{
		db:      db,
		logger:  logger,
		clock:   DefaultClock,
		version: vs,
	}

	if err := store.CheckCurrentMajorSchemaVersion(ctx); err != nil {
		return nil, stacktrace.Propagate(err, "Remote ID schema version check failed")
	}

	return store, nil
}

// CheckCurrentMajorSchemaVersion checks that store supports the current major schema version.
func (s *Store) CheckCurrentMajorSchemaVersion(ctx context.Context) error {
	vs, err := s.GetVersion(ctx)
	if err != nil {
		return stacktrace.Propagate(err, "Failed to get database schema version for remote ID")
	}
	if vs == cockroach.UnknownVersion {
		return stacktrace.NewError("Remote ID database has not been bootstrapped with Schema Manager, Please check https://github.com/interuss/dss/tree/master/build#updgrading-database-schemas")
	}

	if currentMajorSchemaVersion != vs.Major {
		return stacktrace.NewError("Unsupported schema version for remote ID! Got %s, requires major version of %d. Please check https://github.com/interuss/dss/tree/master/build#updgrading-database-schemas", vs, currentMajorSchemaVersion)
	}

	return nil
}

// Interact implements store.Interactor interface.
func (s *Store) Interact(ctx context.Context) (repos.Repository, error) {
	logger := logging.WithValuesFromContext(ctx, s.logger)
	storeVersion, err := s.GetVersion(ctx)
	if err != nil {
		return nil, stacktrace.Propagate(err, "Error determining database RID schema version")
	}

	return &repo{
		ISA:          NewISARepo(ctx, s.db, *storeVersion, logger),
		Subscription: NewISASubscriptionRepo(ctx, s.db, *storeVersion, logger, s.clock),
	}, nil
}

// Transact supplies a new repo, that will perform all of the DB accesses
// in a Txn, and will retry any Txn's that fail due to retry-able errors
// (typically contention).
func (s *Store) Transact(ctx context.Context, f func(repo repos.Repository) error) error {
	logger := logging.WithValuesFromContext(ctx, s.logger)
	// TODO: consider what tx opts we want to support.
	// TODO: we really need to remove the upper cockroach package, and have one
	// "store" for everything
	ctx, cancel := context.WithTimeout(ctx, DefaultTimeout)
	defer cancel()

	storeVersion, err := s.GetVersion(ctx)
	if err != nil {
		return stacktrace.Propagate(err, "Error determining database RID schema version")
	}
	return crdbpgx.ExecuteTx(ctx, s.db, pgx.TxOptions{}, func(tx pgx.Tx) error {
		// Is this recover still necessary?
		defer recoverRollbackRepanic(ctx, tx)
		return f(&repo{
			ISA:          NewISARepo(ctx, tx, *storeVersion, logger),
			Subscription: NewISASubscriptionRepo(ctx, tx, *storeVersion, logger, s.clock),
		})
	})
}

// Close closes the underlying DB connection.
func (s *Store) Close() error {
	s.db.Close()
	return nil
}

func recoverRollbackRepanic(ctx context.Context, tx pgx.Tx) {
	if p := recover(); p != nil {
		if err := tx.Rollback(ctx); err != nil {
			logging.WithValuesFromContext(ctx, logging.Logger).Error(
				"failed to rollback transaction", zap.Error(err),
			)
		}
	}
}

// CleanUp removes all database tables managed by s.
func (s *Store) CleanUp(ctx context.Context) error {
	const query = `
	DELETE FROM subscriptions WHERE id IS NOT NULL;
	DELETE FROM identification_service_areas WHERE id IS NOT NULL;`

	_, err := s.db.Exec(ctx, query)
	return err
}

// GetVersion returns the Version string for the Database.
// If the DB was is not bootstrapped using the schema manager we throw and error
func (s *Store) GetVersion(ctx context.Context) (*semver.Version, error) {
	if s.version == nil {
		vs, err := s.db.GetVersion(ctx, DatabaseName)
		if err != nil {
			return nil, stacktrace.Propagate(err, "Failed to get database schema version for remote ID")
		}
		s.version = vs
	}
	return s.version, nil
}
