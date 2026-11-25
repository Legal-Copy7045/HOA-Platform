#!/bin/sh
# Local S3-compatible object store (decision: SeaweedFS replaces MinIO, whose
# images are no longer published). Production uses Cloudflare R2 instead.
#
# Writes an identities file from the environment so the app's key is limited
# to its own bucket. Anonymous access is denied because identities exist.
set -eu
: "${S3_BUCKET:?}" "${S3_ACCESS_KEY_ID:?}" "${S3_SECRET_ACCESS_KEY:?}"
umask 077
cat > /tmp/s3.json <<JSON
{
  "identities": [
    {
      "name": "hoa-app",
      "credentials": [
        {"accessKey": "${S3_ACCESS_KEY_ID}", "secretKey": "${S3_SECRET_ACCESS_KEY}"}
      ],
      "actions": [
        "Read:${S3_BUCKET}", "Write:${S3_BUCKET}", "List:${S3_BUCKET}",
        "Tagging:${S3_BUCKET}", "Admin:${S3_BUCKET}"
      ]
    }
  ]
}
JSON
# Volumes of 1 GB (SeaweedFS defaults to 30 GB). With -volume.max=0 the
# number of volumes is worked out from free disk space, so on a small disk
# the 30 GB default gave zero volumes and every upload failed with
# InternalError (POC, 2026-11).
exec weed server -dir=/data -ip.bind=0.0.0.0 -s3 -s3.port=8333 -s3.config=/tmp/s3.json \
  -master.volumeSizeLimitMB="${SEAWEEDFS_VOLUME_SIZE_MB:-1024}" -volume.max=0
