"""
rohmu - google cloud object store interface

Copyright (c) 2016 Ohmu Ltd
See LICENSE for details
"""
# pylint: disable=import-error, no-name-in-module

# NOTE: this import is not needed per-se, but it's imported here first to point the
# user to the most important possible missing dependency
import googleapiclient  # noqa pylint: disable=unused-import

from contextlib import contextmanager
from io import BytesIO, FileIO
import dateutil.parser
import httplib2
import json
import logging
import os
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload, MediaIoBaseDownload
from oauth2client import GOOGLE_TOKEN_URI
from oauth2client.client import GoogleCredentials
try:
    from oauth2client.service_account import ServiceAccountCredentials
except ImportError:
    from oauth2client.service_account import _ServiceAccountCredentials as ServiceAccountCredentials

from ..errors import FileNotFoundFromStorageError, InvalidConfigurationError
from .base import BaseTransfer

logging.getLogger("googleapiclient").setLevel(logging.WARNING)
logging.getLogger("oauth2client").setLevel(logging.WARNING)

CHUNK_SIZE = 1024 * 1024 * 5


def unpaginate(domain, initial_op):
    """Iterate thru the request pages until all items have been processed"""
    request = initial_op(domain)
    while request is not None:
        result = request.execute()
        for item in result.get("items", []):
            yield item
        request = domain.list_next(request, result)


def get_credentials(credential_file=None, credentials=None):
    if credential_file:
        return GoogleCredentials.from_stream(credential_file)

    if credentials and credentials["type"] == "service_account":
        return ServiceAccountCredentials(
            service_account_id=credentials["client_id"],
            service_account_email=credentials["client_email"],
            private_key_id=credentials["private_key_id"],
            private_key_pkcs8_text=credentials["private_key"],
            scopes=[])

    if credentials and credentials["type"] == "authorized_user":
        return GoogleCredentials(
            access_token=None,
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
            refresh_token=credentials["refresh_token"],
            token_expiry=None,
            token_uri=GOOGLE_TOKEN_URI,
            user_agent="pghoard")

    return GoogleCredentials.get_application_default()


class GoogleTransfer(BaseTransfer):
    def __init__(self, project_id, bucket_name, credential_file=None, credentials=None, prefix=None):
        super().__init__(prefix=prefix)
        self.project_id = project_id
        self.google_creds = get_credentials(credential_file=credential_file, credentials=credentials)
        self.gs = self._init_google_client()
        self.gs_object_client = None
        self.bucket_name = self.get_or_create_bucket(bucket_name)
        self.log.debug("GoogleTransfer initialized")

    def _init_google_client(self):
        start_time = time.monotonic()
        while True:
            try:
                # sometimes fails: httplib2.ServerNotFoundError: Unable to find the server at www.googleapis.com
                return build("storage", "v1", credentials=self.google_creds)
            except httplib2.ServerNotFoundError:
                if time.monotonic() - start_time > 40.0:
                    raise

            # retry on DNS issues
            time.sleep(1.0)

    @contextmanager
    def _object_client(self, *, not_found=None):
        """(Re-)initialize object client if required, handle 404 errors gracefully and reset the client on
        server errors.  Server errors have been shown to be caused by invalid state in the client and do not
        seem to be resolved without resetting."""
        if self.gs_object_client is None:
            if self.gs is None:
                self.gs = self._init_google_client()
            self.gs_object_client = self.gs.objects()  # pylint: disable=no-member

        try:
            yield self.gs_object_client
        except HttpError as ex:
            if ex.resp["status"] == "404" and not_found is not None:
                raise FileNotFoundFromStorageError(not_found)
            if ex.resp["status"] >= "500" and ex.resp["status"] <= "599":
                self.log.error("Received server error %r, resetting Google API client", ex.resp["status"])
                self.gs = None
                self.gs_object_client = None
            raise

    def get_metadata_for_key(self, key):
        key = self.format_key_for_backend(key)
        with self._object_client(not_found=key) as clob:
            return self._metadata_for_key(clob, key)

    def _metadata_for_key(self, clob, key):
        req = clob.get(bucket=self.bucket_name, object=key)
        obj = req.execute()
        return obj.get("metadata", {})

    def list_path(self, key):
        path = self.format_key_for_backend(key, trailing_slash=True)
        self.log.debug("Listing path %r", path)
        return_list = []
        with self._object_client() as clob:
            for item in unpaginate(clob, lambda o: o.list(bucket=self.bucket_name, delimiter="/", prefix=path)):
                if item["name"].endswith("/"):
                    continue  # skip directory level objects

                return_list.append({
                    "name": self.format_key_from_backend(item["name"]),
                    "size": int(item["size"]),
                    "last_modified": dateutil.parser.parse(item["updated"]),
                    "metadata": item.get("metadata", {}),
                })
        return return_list

    def delete_key(self, key):
        key = self.format_key_for_backend(key)
        self.log.debug("Deleting key: %r", key)
        with self._object_client(not_found=key) as clob:
            req = clob.delete(bucket=self.bucket_name, object=key)
            req.execute()

    def get_contents_to_file(self, key, filepath_to_store_to, *, progress_callback=None):
        fileobj = FileIO(filepath_to_store_to, mode="wb")
        done = False
        metadata = {}
        try:
            metadata = self.get_contents_to_fileobj(key, fileobj, progress_callback=progress_callback)
            done = True
        finally:
            fileobj.close()
            if not done:
                os.unlink(filepath_to_store_to)
        return metadata

    def get_contents_to_fileobj(self, key, fileobj_to_store_to, *, progress_callback=None):
        key = self.format_key_for_backend(key)
        self.log.debug("Starting to fetch the contents of: %r to %r", key, fileobj_to_store_to)
        next_prog_report = 0
        with self._object_client(not_found=key) as clob:
            req = clob.get_media(bucket=self.bucket_name, object=key)
            download = MediaIoBaseDownload(fileobj_to_store_to, req, chunksize=CHUNK_SIZE)
            done = False
            while not done:
                status, done = download.next_chunk()
                if status:
                    progress_pct = status.progress() * 100
                    self.log.debug("Download of %r: %d%%", key, progress_pct)
                    if progress_callback and progress_pct > next_prog_report:
                        progress_callback(progress_pct, 100)
                        next_prog_report = progress_pct + 0.1
            return self._metadata_for_key(clob, key)

    def get_contents_to_string(self, key):
        key = self.format_key_for_backend(key)
        self.log.debug("Starting to fetch the contents of: %r", key)
        with self._object_client(not_found=key) as clob:
            req = clob.get_media(bucket=self.bucket_name, object=key)
            data = req.execute()
            return data, self._metadata_for_key(clob, key)

    def _upload(self, upload_type, local_object, key, metadata, extra_props):
        key = self.format_key_for_backend(key)
        self.log.debug("Starting to upload %r", key)
        upload = upload_type(local_object, mimetype="application/octet-stream",
                             resumable=True, chunksize=CHUNK_SIZE)
        body = {"metadata": metadata}
        if extra_props:
            body.update(extra_props)

        with self._object_client() as clob:
            req = clob.insert(bucket=self.bucket_name, name=key, media_body=upload, body=body)
            response = None
            while response is None:
                status, response = req.next_chunk()
                if status:
                    self.log.debug("Upload of %r to %r: %d%%", local_object, key, status.progress() * 100)

    def store_file_from_memory(self, key, memstring, metadata=None, extra_props=None):  # pylint: disable=arguments-differ
        return self._upload(MediaIoBaseUpload, BytesIO(memstring), key,
                            self.sanitize_metadata(metadata), extra_props)

    def store_file_from_disk(self, key, filepath, metadata=None,  # pylint: disable=arguments-differ, unused-variable
                             *, multipart=None, extra_props=None):  # pylint: disable=arguments-differ, unused-variable
        return self._upload(MediaFileUpload, filepath, key, self.sanitize_metadata(metadata), extra_props)

    def get_or_create_bucket(self, bucket_name):
        """Look up the bucket if it already exists and try to create the
        bucket in case it doesn't.  Note that we can't just always try to
        unconditionally create the bucket as Google imposes a strict rate
        limit on bucket creation operations, even if it doesn't result in a
        new bucket.

        Quietly handle the case where the bucket already exists to avoid
        race conditions.  Note that we'll get a 400 Bad Request response for
        invalid bucket names ("Invalid bucket name") as well as for invalid
        project ("Invalid argument"), try to handle both gracefully."""
        start_time = time.time()
        gs_buckets = self.gs.buckets()  # pylint: disable=no-member
        try:
            gs_buckets.get(bucket=bucket_name).execute()
            self.log.debug("Bucket: %r already exists, took: %.3fs", bucket_name, time.time() - start_time)
        except HttpError as ex:
            if ex.resp["status"] == "404":
                pass  # we need to create it
            elif ex.resp["status"] == "403":
                raise InvalidConfigurationError("Bucket {0!r} exists but isn't accessible".format(bucket_name))
            else:
                raise
        else:
            return bucket_name

        try:
            req = gs_buckets.insert(project=self.project_id, body={"name": bucket_name})
            req.execute()
            self.log.debug("Created bucket: %r successfully, took: %.3fs", bucket_name, time.time() - start_time)
        except HttpError as ex:
            error = json.loads(ex.content.decode("utf-8"))["error"]
            if error["message"].startswith("You already own this bucket"):
                self.log.debug("Bucket: %r already exists, took: %.3fs", bucket_name, time.time() - start_time)
            elif error["message"] == "Invalid argument.":
                raise InvalidConfigurationError("Invalid project id {0!r}".format(self.project_id))
            elif error["message"].startswith("Invalid bucket name"):
                raise InvalidConfigurationError("Invalid bucket name {0!r}".format(bucket_name))
            else:
                raise

        return bucket_name
