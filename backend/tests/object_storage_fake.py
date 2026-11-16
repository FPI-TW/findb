"""An in-memory stand-in for the S3 calls ``app.services.object_storage`` makes."""

from __future__ import annotations

import hashlib
import io

from botocore.exceptions import ClientError

SECRET = "secret-that-must-not-leak"


def client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": SECRET},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Operation",
    )


class FakeR2:
    """The S3 calls the module uses, over an in-memory bucket."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.declared_length: int | None = None
        self.failure: Exception | None = None

    def _etag(self, body: bytes) -> str:
        return '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'

    def get_object(self, **arguments):
        self.calls.append(("get", arguments))
        if self.failure is not None:
            raise self.failure
        body = self.objects.get((arguments["Bucket"], arguments["Key"]))
        if body is None:
            raise client_error("NoSuchKey", 404)
        length = len(body) if self.declared_length is None else self.declared_length
        return {"ContentLength": length, "Body": io.BytesIO(body), "ETag": self._etag(body)}

    def head_object(self, **arguments):
        self.calls.append(("head", arguments))
        if self.failure is not None:
            raise self.failure
        body = self.objects.get((arguments["Bucket"], arguments["Key"]))
        if body is None:
            raise client_error("404", 404)
        return {"ContentLength": len(body), "ETag": self._etag(body)}

    def put_object(self, **arguments):
        self.calls.append(("put", arguments))
        if self.failure is not None:
            raise self.failure
        slot = (arguments["Bucket"], arguments["Key"])
        current = self.objects.get(slot)
        if arguments.get("IfNoneMatch") == "*" and current is not None:
            raise client_error("PreconditionFailed", 412)
        if "IfMatch" in arguments and (
            current is None or self._etag(current) != arguments["IfMatch"]
        ):
            raise client_error("PreconditionFailed", 412)
        self.objects[slot] = arguments["Body"]
        return {"ETag": self._etag(arguments["Body"])}

    def delete_object(self, **arguments):
        self.calls.append(("delete", arguments))
        if self.failure is not None:
            raise self.failure
        self.objects.pop((arguments["Bucket"], arguments["Key"]), None)
        return {}
