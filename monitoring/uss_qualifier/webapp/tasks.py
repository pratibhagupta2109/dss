import json
from typing import List
import redis
import rq
import uuid
from . import resources
from monitoring.monitorlib.typing import ImplicitDict
from monitoring.uss_qualifier.rid import test_executor
from monitoring.uss_qualifier.rid.utils import RIDQualifierTestConfiguration, FullFlightRecord
from monitoring.uss_qualifier.scd.configuration import SCDQualifierTestConfiguration
from monitoring.uss_qualifier.rid.simulator import flight_state_from_kml
from monitoring.uss_qualifier.test_data import test_report


def get_rq_job(job_id):
    try:
        rq_job = resources.qualifier_queue.fetch_job(job_id)
    except (redis.exceptions.RedisError, rq.exceptions.NoSuchJobError):
        return None
    return rq_job


def remove_rq_job(job_id):
    """Removes a job from the queue."""
    try:
        rq_job = resources.qualifier_queue.remove(job_id)
    except (redis.exceptions.RedisError, rq.exceptions.NoSuchJobError):
        return None
    return rq_job


def call_test_executor(
        user_config_json: str,
        auth_spec: str,
        flight_record_jsons: List[str],
        testruns_id,
        debug=False):

    usr_config = json.loads(user_config_json)
    if usr_config.get('rid'):
        user_config: RIDQualifierTestConfiguration = ImplicitDict.parse(
            usr_config['rid'], RIDQualifierTestConfiguration)
    elif usr_config.get('scd'):
        user_config: SCDQualifierTestConfiguration = ImplicitDict.parse(
            usr_config['scd'], SCDQualifierTestConfiguration)
    flight_records: List[FullFlightRecord] = [
        ImplicitDict.parse(json.loads(j), FullFlightRecord)
        for j in flight_record_jsons]
    if debug:
        report = test_report.test_data
    else:
        report = test_executor.run_rid_tests(
            user_config, auth_spec, flight_records)
    resources.redis_conn.hset(
        resources.REDIS_KEY_TEST_RUNS, testruns_id, json.dumps(report))
    return json.dumps(report)


def call_kml_processor(kml_content, output_path):
    flight_states = flight_state_from_kml.main(
        kml_content, output_path, from_string=True)
    resources.redis_conn.hset(
        resources.REDIS_KEY_UPLOADED_KMLS, str(uuid.uuid4()), json.dumps(flight_states))
    return flight_states
