"""Upload helper for OCI Object Storage.

Uses the standard OCI Python SDK. Works with either a config-file profile
(~/.oci/config, simplest for a personal VM) or instance principal auth if the
connector ever runs on an OCI compute instance with a dynamic group policy
attached (no key files needed in that case).
"""
import gzip
import json
import logging

import oci

log = logging.getLogger("oci_uploader")


class ObjectStorageUploader:
    def __init__(self, namespace: str, bucket: str, region: str, profile: str = "DEFAULT",
                 use_instance_principal: bool = False):
        if use_instance_principal:
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            self.client = oci.object_storage.ObjectStorageClient(config={}, signer=signer)
        else:
            config = oci.config.from_file(profile_name=profile)
            config["region"] = region or config.get("region")
            oci.config.validate_config(config)
            self.client = oci.object_storage.ObjectStorageClient(config)

        self.namespace = namespace
        self.bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self):
        try:
            self.client.head_bucket(namespace_name=self.namespace, bucket_name=self.bucket)
        except oci.exceptions.ServiceError as e:
            if e.status == 404:
                raise RuntimeError(
                    f"Bucket '{self.bucket}' not found in namespace '{self.namespace}'. "
                    "Create it first (see setup guide) before starting the connector."
                ) from e
            raise

    def upload_json(self, object_name: str, data: dict, compress: bool = False) -> str:
        """Upload a dict as JSON. When compress=True, the object is gzipped
        before upload and content_encoding is set to 'gzip' so browsers/CLIs
        that respect that header (curl --compressed, browsers) transparently
        decompress it. Object storage still charges for the compressed size,
        which is the point — transcript JSON compresses very well (~80-90%
        smaller), stretching the Always Free 20 GB allowance considerably
        further. The caller is responsible for naming the object with a
        .json.gz (compressed) or .json (uncompressed) suffix so it's obvious
        from the object name alone which one it is."""
        raw = json.dumps(data, indent=2, default=str).encode("utf-8")
        if compress:
            body = gzip.compress(raw)
            content_type = "application/json"
            content_encoding = "gzip"
        else:
            body = raw
            content_type = "application/json"
            content_encoding = None

        kwargs = dict(
            namespace_name=self.namespace,
            bucket_name=self.bucket,
            object_name=object_name,
            put_object_body=body,
            content_type=content_type,
        )
        if content_encoding:
            kwargs["content_encoding"] = content_encoding

        self.client.put_object(**kwargs)
        log.info(
            "Uploaded %s (%d bytes%s) to bucket %s",
            object_name, len(body), " gzip-compressed" if compress else "", self.bucket,
        )
        return object_name

    def upload_from_url(self, object_name: str, source_url: str, content_type: str = "application/octet-stream") -> str:
        """Stream a remote file (e.g. Fireflies audio/video URL) straight into Object Storage
        without buffering the whole thing in memory."""
        import httpx

        with httpx.stream("GET", source_url, timeout=120.0, follow_redirects=True) as r:
            r.raise_for_status()
            self.client.put_object(
                namespace_name=self.namespace,
                bucket_name=self.bucket,
                object_name=object_name,
                put_object_body=r.iter_bytes(),
                content_type=content_type,
            )
        log.info("Streamed %s from source URL to bucket %s", object_name, self.bucket)
        return object_name

    def object_exists(self, object_name: str) -> bool:
        try:
            self.client.head_object(
                namespace_name=self.namespace, bucket_name=self.bucket, object_name=object_name
            )
            return True
        except oci.exceptions.ServiceError as e:
            if e.status == 404:
                return False
            raise
