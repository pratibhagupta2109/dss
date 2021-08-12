import flask
import os
import pathlib
import requests
import logging

from . import config
from . import forms
from . import tasks

from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
import google.auth.transport.requests
from flask import render_template, request, make_response, redirect, url_for, session, jsonify
from functools import wraps
from pip._vendor import cachecontrol
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from monitoring.monitorlib import versioning, auth_validation
from monitoring.rid_qualifier.host import webapp

logging.basicConfig(level=logging.DEBUG)

client_secrets_file = os.path.join(
    pathlib.Path(__file__).parent,
    'client_secret.json')

flow = Flow.from_client_secrets_file(
    client_secrets_file=client_secrets_file,
    scopes=[
        'https://www.googleapis.com/auth/userinfo.profile',
        'https://www.googleapis.com/auth/userinfo.email',
        'openid'],
    redirect_uri=f'{config.Config.RID_HOST_URL}/callback')


def login_required(function):
    @wraps(function)
    def decorated_function(*args, **kwargs):
        if 'google_id' not in session:
            return redirect(url_for('login', next=request.url))
        return function(*args, **kwargs)
    return decorated_function


@webapp.route('/login')
def login():
    # make sure session is empty
    session.clear()
    authorization_url, state = flow.authorization_url()
    session['state'] = state
    return redirect(authorization_url)


@webapp.route('/callback')
def callback():
    flow.fetch_token(authorization_response=request.url)

    credentials = flow.credentials
    request_session = requests.session()
    cached_session = cachecontrol.CacheControl(request_session)
    token_request = google.auth.transport.requests.Request(
        session=cached_session)

    id_info = id_token.verify_oauth2_token(
        id_token=credentials._id_token,
        request=token_request,
        audience=config.Config.GOOGLE_CLIENT_ID
    )

    session['google_id'] = id_info.get('sub')
    session['name'] = id_info.get('name')
    return redirect('/')


@webapp.route('/logout')
def logout():
    session.clear()
    return render_template('logout.html')


@webapp.route('/')
@login_required
def home_page():
    if session.get('google_id'):
        return render_template(
            'home.html',
            title='Home',
            greetings='Hello RID Host !!')
    return redirect('/login')


def start_background_task(user_config, auth_spec, input_files, debug):
    job = config.Config.qualifier_queue.enqueue(
        'monitoring.rid_qualifier.host.tasks.call_test_executor',
        user_config, auth_spec, input_files, debug)
    return job.get_id()


@webapp.route('/executor', methods=['GET', 'POST'])
@login_required
def execute_task():
    files = request.args['files']
    if not files:
        return {'error' :'files not found.'}
    files = files.split(',')
    form = forms.UserConfig(file_count=len(files))
    job_id = ''
    data = {}
    if form.validate_on_submit():
      file_objs = []
      for f in files:
        with open(f) as fo:
          file_objs.append(fo.read())
      job_id = start_background_task(
          form.user_config.data,
          form.auth_spec.data,
          file_objs,
          form.sample_report.data)
    if request.method == 'POST':
        data = {
            'job_id': job_id
        }
    # return render_template(
    #     'start_task.html',
    #     title='Get User config',
    #     form=form,
    #     data=data)
    return render_template(
        'user_specs.html',
        title='Get User config',
        form=form,
        data=data)


@webapp.route('/result/<string:job_id>', methods=['GET', 'POST'])
def get_result(job_id):
    task = tasks.get_rq_job(job_id)
    response_object = {}
    if task:
        response_object = {
            'task_id': task.get_id(),
            'task_status': task.get_status(),
            'task_result': task.result,
        }
    if task.get_status() == 'finished':
      if task.result:
        filename = 'report.json'
        filepath = f'{config.Config.FILE_PATH}/user_name/tests/{filename}'
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
          f.write(task.result)
        response_object.update({'filename': filename})
      else:
        logging.info('Task result not available yet..')
    return response_object


@webapp.route('/report/<string:job_id>', methods=['POST'])
def get_report(job_id):
    response_object = config.Config.redis_client.get(job_id)
    output = make_response(response_object)
    output.headers['Content-Disposition'] = 'attachment; filename=report.json'
    output.headers['Content-type'] = 'text/csv'
    return output


@webapp.route('/upload_file', methods=['POST'])
def upload_flight_state_files():
    """Upload files."""
    files = request.files.getlist('files')
    destination_file_paths = []
    folder_path = f'{config.Config.FILE_PATH}/user_name/flight_records'
    if not os.path.isdir(folder_path):
        os.makedirs(folder_path)
    for file in files:
        if file:
            filename = secure_filename(file.filename)
            if filename.endswith('.json'):
                file_path = os.path.join(folder_path, filename)
                file.save(file_path)
                destination_file_paths.append(file_path)
    # return redirect(
    #     url_for(
    #         '.execute_task',
    #         files=','.join(destination_file_paths)))
    # return redirect(
    #     url_for('.tests')
    # )
    # return render_template('flight_records.html')
    return {'uploaded_files': [f.filename for f in files]}


@webapp.route('/tests',  methods=['GET'])
def tests():
    return render_template('tests2.html', data=None)


@webapp.route('/flight-records', methods=['POST'])
def get_flight_records():
    data = {
        'flight_records': [],
        'message': ''
    }
    folder_path = f'{config.Config.FILE_PATH}/user_name/flight_records'
    if not os.path.isdir(folder_path):
        data['message'] = 'Flight records not available.'
    else:
        flight_records = [f for f in os.listdir(folder_path) if f.endswith('.json')]
        data['flight_records'] = flight_records
    return render_template('flight_records.html', data=data)
    # return render_template('tests2.html', flight_data=data)
    return jsonify(data)
    

@webapp.route('/status')
def status():
    return 'Mock Host Service Provider ok {}'.format(
        versioning.get_code_version())


@webapp.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    elif isinstance(e, auth_validation.InvalidScopeError):
        return flask.jsonify({
            'message': 'Invalid scope; expected one of {%s}, but received only {%s}' % (
                ' '.join(e.permitted_scopes),
                ' '.join(e.provided_scopes))}), 403
    elif isinstance(e, auth_validation.InvalidAccessTokenError):
        return flask.jsonify({'message': e.message}), 401
    elif isinstance(e, auth_validation.ConfigurationError):
        return flask.jsonify({'message': e.message}), 500
    elif isinstance(e, ValueError):
        return flask.jsonify({'message': str(e)}), 400

    return flask.jsonify({'message': str(e)}), 500
