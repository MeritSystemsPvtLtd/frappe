# -*- coding: utf-8 -*-
# Copyright (c) 2015, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import os
from frappe import _
from frappe.model.document import Document
import dropbox, json
from frappe.utils.backups import new_backup
from frappe.utils.background_jobs import enqueue
from six.moves.urllib.parse import urlparse, parse_qs
from frappe.integrations.utils import make_post_request
from rq.timeouts import JobTimeoutException
from frappe.utils import (cint, split_emails, get_request_site_address,
	get_files_path, get_backups_path, get_url, encode)
from six import text_type

ignore_list = [".DS_Store"]

class DropboxSettings(Document):
	def onload(self):
		if not self.app_access_key and frappe.conf.dropbox_access_key:
			self.set_onload("dropbox_setup_via_site_config", 1)

@frappe.whitelist()
def take_backup():
	"Enqueue longjob for taking backup to dropbox"
	enqueue("frappe.integrations.doctype.dropbox_settings.dropbox_settings.take_backup_to_dropbox", queue='long', timeout=1500)
	frappe.msgprint(_("Queued for backup. It may take a few minutes to an hour."))

def take_backups_daily():
	take_backups_if("Daily")

def take_backups_weekly():
	take_backups_if("Weekly")

def take_backups_if(freq):
	if frappe.db.get_value("Dropbox Settings", None, "backup_frequency") == freq:
		take_backup_to_dropbox()

def take_backup_to_dropbox(retry_count=0, upload_db_backup=True):
	did_not_upload, error_log = [], []
	try:
		if cint(frappe.db.get_value("Dropbox Settings", None, "enabled")):
			did_not_upload, error_log = backup_to_dropbox(upload_db_backup)
			if did_not_upload: raise Exception

			send_email(True, "Dropbox")
	except JobTimeoutException:
		if retry_count < 2:
			args = {
				"retry_count": retry_count + 1,
				"upload_db_backup": False #considering till worker timeout db backup is uploaded
			}
			enqueue("frappe.integrations.doctype.dropbox_settings.dropbox_settings.take_backup_to_dropbox",
				queue='long', timeout=1500, **args)
	except Exception:
		file_and_error = [" - ".join(f) for f in zip(did_not_upload, error_log)]
		error_message = ("\n".join(file_and_error) + "\n" + frappe.get_traceback())
		frappe.errprint(error_message)
		send_email(False, "Dropbox", error_message)

def send_email(success, service_name, error_status=None):
	if success:
		subject = "Backup Upload Successful"
		message ="""<h3>Backup Uploaded Successfully</h3><p>Hi there, this is just to inform you
		that your backup was successfully uploaded to your %s account. So relax!</p>
		""" % service_name

	else:
		subject = "[Warning] Backup Upload Failed"
		message ="""<h3>Backup Upload Failed</h3><p>Oops, your automated backup to %s
		failed.</p>
		<p>Error message: <br>
		<pre><code>%s</code></pre>
		</p>
		<p>Please contact your system manager for more information.</p>
		""" % (service_name, error_status)

	if not frappe.db:
		frappe.connect()

	recipients = split_emails(frappe.db.get_value("Dropbox Settings", None, "send_notifications_to"))
	frappe.sendmail(recipients=recipients, subject=subject, message=message)

def backup_to_dropbox(upload_db_backup=True):
	if not frappe.db:
		frappe.connect()

	# upload database
	dropbox_settings = get_dropbox_settings()

	if not dropbox_settings['access_token']:
		access_token = generate_oauth2_access_token_from_oauth1_token(dropbox_settings)

		if not access_token.get('oauth2_token'):
			return 'Failed backup upload', 'No Access Token exists! Please generate the access token for Dropbox.'

		dropbox_settings['access_token'] = access_token['oauth2_token']
		set_dropbox_access_token(access_token['oauth2_token'])

	dropbox_client = dropbox.Dropbox(dropbox_settings['access_token'])

	if not upload_db_backup:
		backup = new_backup(ignore_files=True)
		filename = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_db))
		upload_file_to_dropbox(filename, "/database", dropbox_client)

	# upload files to files folder
	did_not_upload = []
	error_log = []

	upload_from_folder(get_files_path(), 0, "/files", dropbox_client, did_not_upload, error_log)
	upload_from_folder(get_files_path(is_private=1), 1, "/private/files", dropbox_client, did_not_upload, error_log)

	return did_not_upload, list(set(error_log))

def upload_from_folder(path, is_private, dropbox_folder, dropbox_client, did_not_upload, error_log):
	if not os.path.exists(path):
		return

	if is_fresh_upload():
		response = get_uploaded_files_meta(dropbox_folder, dropbox_client)
	else:
		response = frappe._dict({"entries": []})

	path = text_type(path)

	for f in frappe.get_all("File", filters={"is_folder": 0, "is_private": is_private,
		"uploaded_to_dropbox": 0}, fields=['file_url', 'name']):

		filename = f.file_url.replace('/files/', '')
		filepath = os.path.join(path, filename)

		if filename in ignore_list:
			continue

		found = False
		for file_metadata in response.entries:
			if (os.path.basename(filepath) == file_metadata.name
				and os.stat(encode(filepath)).st_size == int(file_metadata.size)):
				found = True
				update_file_dropbox_status(f.name)
				break

		if not found:
			try:
				upload_file_to_dropbox(filepath, dropbox_folder, dropbox_client)
				update_file_dropbox_status(f.name)
			except Exception:
				did_not_upload.append(filepath)
				error_log.append(frappe.get_traceback())

def upload_file_to_dropbox(filename, folder, dropbox_client):
	"""upload files with chunk of 15 mb to reduce session append calls"""

	create_folder_if_not_exists(folder, dropbox_client)
	chunk_size = 15 * 1024 * 1024
	file_size = os.path.getsize(encode(filename))
	mode = (dropbox.files.WriteMode.overwrite)

	if not os.path.exists(filename):
		return

	f = open(encode(filename), 'rb')
	path = "{0}/{1}".format(folder, os.path.basename(filename))

	try:
		if file_size <= chunk_size:
			dropbox_client.files_upload(f.read(), path, mode)
		else:
			upload_session_start_result = dropbox_client.files_upload_session_start(f.read(chunk_size))
			cursor = dropbox.files.UploadSessionCursor(session_id=upload_session_start_result.session_id, offset=f.tell())
			commit = dropbox.files.CommitInfo(path=path, mode=mode)

			while f.tell() < file_size:
				if ((file_size - f.tell()) <= chunk_size):
					dropbox_client.files_upload_session_finish(f.read(chunk_size), cursor, commit)
				else:
					dropbox_client.files_upload_session_append(f.read(chunk_size), cursor.session_id,cursor.offset)
					cursor.offset = f.tell()
	except dropbox.exceptions.ApiError as e:
		if isinstance(e.error, dropbox.files.UploadError):
			error = "File Path: {path}\n".format(path=path)
			error += frappe.get_traceback()
			frappe.log_error(error)
		else:
			raise

def create_folder_if_not_exists(folder, dropbox_client):
	try:
		dropbox_client.files_get_metadata(folder)
	except dropbox.exceptions.ApiError as e:
		# folder not found
		if isinstance(e.error, dropbox.files.GetMetadataError):
			dropbox_client.files_create_folder(folder)
		else:
			raise

def update_file_dropbox_status(file_name):
	frappe.db.set_value("File", file_name, 'uploaded_to_dropbox', 1, update_modified=False)

def is_fresh_upload():
	file_name = frappe.db.get_value("File", {'uploaded_to_dropbox': 1}, 'name')
	return not file_name

def get_uploaded_files_meta(dropbox_folder, dropbox_client):
	try:
		return dropbox_client.files_list_folder(dropbox_folder)
	except dropbox.exceptions.ApiError as e:
		# folder not found
		if isinstance(e.error, dropbox.files.ListFolderError):
			return frappe._dict({"entries": []})
		else:
			raise 

def get_dropbox_settings(redirect_uri=False):
	settings = frappe.get_doc("Dropbox Settings")
	app_details = {
		"app_key": settings.app_access_key or frappe.conf.dropbox_access_key,
		"app_secret": settings.get_password(fieldname="app_secret_key", raise_exception=False)
			if settings.app_secret_key else frappe.conf.dropbox_secret_key,
		'access_token': settings.get_password('dropbox_access_token', raise_exception=False)
			if settings.dropbox_access_token else '',
		'access_key': settings.get_password('dropbox_access_key', raise_exception=False),
		'access_secret': settings.get_password('dropbox_access_secret', raise_exception=False)
	}

	if redirect_uri:
		app_details.update({
			'redirect_uri': get_request_site_address(True) \
				+ '/api/method/frappe.integrations.doctype.dropbox_settings.dropbox_settings.dropbox_auth_finish' \
				if settings.app_secret_key else frappe.conf.dropbox_broker_site\
				+ '/api/method/dropbox_erpnext_broker.www.setup_dropbox.generate_dropbox_access_token',
		})

	if not app_details['app_key'] or not app_details['app_secret']:
		raise Exception(_("Please set Dropbox access keys in your site config"))

	return app_details

@frappe.whitelist()
def get_redirect_url():
	url = "{0}/api/method/dropbox_erpnext_broker.www.setup_dropbox.get_authotize_url".format(frappe.conf.dropbox_broker_site)

	try:
		response = make_post_request(url, data={"site": get_url()})
		if response.get("message"):
			return response["message"]

	except Exception:
		frappe.log_error()
		frappe.throw(
			_("Something went wrong while generating dropbox access token. Please check error log for more details.")
		)

@frappe.whitelist()
def get_dropbox_authorize_url():
	app_details = get_dropbox_settings(redirect_uri=True)
	dropbox_oauth_flow = dropbox.DropboxOAuth2Flow(
		app_details["app_key"],
		app_details["app_secret"],
		app_details["redirect_uri"],
		{},
		"dropbox-auth-csrf-token"
	)

	auth_url = dropbox_oauth_flow.start()

	return {
		"auth_url": auth_url,
		"args": parse_qs(urlparse(auth_url).query)
	}

@frappe.whitelist()
def dropbox_auth_finish(return_access_token=False):
	app_details = get_dropbox_settings(redirect_uri=True)
	callback = frappe.form_dict
	close = '<p class="text-muted">' + _('Please close this window') + '</p>'

	dropbox_oauth_flow = dropbox.DropboxOAuth2Flow(
		app_details["app_key"],
		app_details["app_secret"],
		app_details["redirect_uri"],
		{
			'dropbox-auth-csrf-token': callback.state
		},
		"dropbox-auth-csrf-token"
	)

	if callback.state or callback.code:
		token = dropbox_oauth_flow.finish({'state': callback.state, 'code': callback.code})
		if return_access_token and token.access_token:
			return token.access_token, callback.state

		set_dropbox_access_token(token.access_token)
	else:
		frappe.respond_as_web_page(_("Dropbox Setup"),
			_("Illegal Access Token. Please try again") + close,
			indicator_color='red',
			http_status_code=frappe.AuthenticationError.http_status_code)

	frappe.respond_as_web_page(_("Dropbox Setup"),
		_("Dropbox access is approved!") + close,
		indicator_color='green')

@frappe.whitelist(allow_guest=True)
def set_dropbox_access_token(access_token):
	frappe.db.set_value("Dropbox Settings", None, 'dropbox_access_token', access_token)
	frappe.db.commit()

def generate_oauth2_access_token_from_oauth1_token(dropbox_settings=None):
	if not dropbox_settings.get("access_key") or not dropbox_settings.get("access_secret"):
		return {}

	url = "https://api.dropboxapi.com/2/auth/token/from_oauth1"
	headers = {"Content-Type": "application/json"}
	auth = (dropbox_settings["app_key"], dropbox_settings["app_secret"])
	data = {
		"oauth1_token": dropbox_settings["access_key"],
		"oauth1_token_secret": dropbox_settings["access_secret"]
	}

	return make_post_request(url, auth=auth, headers=headers, data=json.dumps(data))
