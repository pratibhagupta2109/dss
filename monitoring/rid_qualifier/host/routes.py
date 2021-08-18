import flask
import os
import pathlib
import requests
import logging

from . import config
from . import forms
from . import tasks

from datetime import datetime
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


def start_background_task(user_config, auth_spec, input_files, debug):
    job = config.Config.qualifier_queue.enqueue(
        'monitoring.rid_qualifier.host.tasks.call_test_executor',
        user_config, auth_spec, input_files, debug)
    return job.get_id()


@webapp.route('/', methods=['GET', 'POST'])
@webapp.route('/tests', methods=['GET', 'POST'])
@login_required
def tests():
    flight_record_data = get_flight_records()
    files = []
    if flight_record_data.get('flight_records'):
        files= [(x, x) for x in flight_record_data['flight_records']]

    form = forms.TestsExecuteForm()
    form.flight_records.choices = files
    data = get_test_history()
    job_id = ''
    if form.validate_on_submit():
        file_objs = []
        user_id = session['google_id']
        input_files_location = f'{config.Config.FILE_PATH}/{user_id}/flight_records'
        for filename in form.flight_records.data:
            filepath = f'{input_files_location}/{filename}'
            with open(filepath) as fo:
                file_objs.append(fo.read())
            job_id = start_background_task(
                form.user_config.data,
                form.auth_spec.data,
                file_objs,
                form.sample_report.data)
        if request.method == 'POST':
            data.update({
                'job_id': job_id
            })
    return render_template(
        'tests.html',
        title='Execute tests',
        form=form,
        data=data)


def get_flight_records():
    data = {
        'flight_records': [],
        'message': ''
    }
    user_id = session['google_id']
    folder_path = f'{config.Config.FILE_PATH}/{user_id}/flight_records'
    if not os.path.isdir(folder_path):
        data['message'] = 'Flight records not available.'
    else:
        flight_records = [f for f in os.listdir(folder_path) if f.endswith('.json')]
        data['flight_records'] = flight_records
    return data


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
        now = datetime.now()
        if task.result:
            filename = f'{str(now.date())}_{now.strftime("%H%M%S")}.json'
            user_id = session['google_id']
            filepath = f'{config.Config.FILE_PATH}/{user_id}/tests/{filename}'
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, 'w') as f:
                f.write(task.result)
            response_object.update({'filename': filename})
        else:
            logging.info('Task result not available yet..')
    return response_object


@webapp.route('/report', methods=['POST'])
def get_report():
    user_id = session['google_id']
    output_path = f'{config.Config.FILE_PATH}/{user_id}/tests'
    try:
        latest_file = os.listdir(output_path)[0]
        filepath = f'{config.Config.FILE_PATH}/{user_id}/tests/{latest_file}'
        with open(filepath) as f:
            content = f.read()
            if content:
                output = make_response(content)
                output.headers['Content-Disposition'] = f'attachment; filename={latest_file}'
                output.headers['Content-type'] = 'text/csv'
                return output
    except:
        return {'error': 'Error downloading file'}
    return {'Error': 'Error getting result'}


@webapp.route('/result_download/<string:filename>', methods=['POST', 'GET'])
def download_test(filename):
    user_id = session['google_id']
    filepath = f'{config.Config.FILE_PATH}/{user_id}/tests/{filename}'
    content = ''
    with open(filepath) as f:
        content = f.read()
    if content:
        output = make_response(content)
        output.headers['Content-Disposition'] = f'attachment; filename={filename}'
        output.headers['Content-type'] = 'text/csv'
        return output
    return {'error': 'Error downloading file'}

@webapp.route('/history')
def get_test_history():
    user_id = session['google_id']
    output_path = f'{config.Config.FILE_PATH}/{user_id}/tests'
    try:
        executed_tests = os.listdir(output_path)
    except:
        executed_tests = []
    return {'tests': executed_tests}


@webapp.route('/upload_file', methods=['POST'])
def upload_flight_state_files():
    """Upload files."""
    files = request.files.getlist('files')
    destination_file_paths = []
    user_id = session['google_id']
    folder_path = f'{config.Config.FILE_PATH}/{user_id}/flight_records'
    if not os.path.isdir(folder_path):
        os.makedirs(folder_path)
    for file in files:
        if file:
            filename = secure_filename(file.filename)
            if filename.endswith('.json'):
                file_path = os.path.join(folder_path, filename)
                file.save(file_path)
                destination_file_paths.append(file_path)

    return redirect(url_for('.tests'))


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
