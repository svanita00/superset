# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=import-outside-toplevel, unused-argument
from __future__ import annotations

import time
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from flask.ctx import AppContext

from superset.extensions import db
from superset.key_value.types import JsonKeyValueCodec, KeyValueResource
from superset.key_value.utils import to_naive_utc, utcnow

RESOURCE = KeyValueResource.METASTORE_CACHE
CODEC = JsonKeyValueCodec()
VALUE = {"foo": "bar"}

# Timezones west and east of UTC, so a naive local timestamp is respectively
# behind and ahead of the corresponding UTC timestamp.
NON_UTC_TIMEZONES = ["America/Los_Angeles", "Asia/Kolkata"]


@pytest.fixture(params=NON_UTC_TIMEZONES)
def non_utc_timezone(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[str, None, None]:
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def clean_key_value_store(app_context: AppContext) -> Generator[None, None, None]:
    from superset.key_value.models import KeyValueEntry

    db.session.query(KeyValueEntry).delete()
    db.session.commit()  # pylint: disable=consider-using-transaction
    yield
    db.session.query(KeyValueEntry).delete()
    db.session.commit()  # pylint: disable=consider-using-transaction


def test_utcnow_is_naive_utc(non_utc_timezone: str) -> None:
    """utcnow() tracks UTC, not the process timezone."""
    now = utcnow()
    assert now.tzinfo is None
    assert abs(now - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(
        seconds=5
    )


def test_to_naive_utc_converts_aware_values(non_utc_timezone: str) -> None:
    """Aware timestamps are converted to UTC before the tzinfo is dropped."""
    aware = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5)))
    assert to_naive_utc(aware) == datetime(2024, 1, 2, 8, 4, 5)
    naive = datetime(2024, 1, 2, 3, 4, 5)
    assert to_naive_utc(naive) == naive
    assert to_naive_utc(None) is None


def test_legacy_naive_expiry_is_treated_as_utc(
    non_utc_timezone: str,
    clean_key_value_store: None,
) -> None:
    """Rows holding a naive ``expires_on`` are compared against UTC."""
    from superset.key_value.models import KeyValueEntry

    live = KeyValueEntry(
        resource=RESOURCE,
        value=CODEC.encode(VALUE),
        expires_on=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(minutes=5),
    )
    expired = KeyValueEntry(
        resource=RESOURCE,
        value=CODEC.encode(VALUE),
        expires_on=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=5),
    )
    assert live.is_expired() is False
    assert expired.is_expired() is True


def test_metastore_cache_honors_ttl(
    non_utc_timezone: str,
    clean_key_value_store: None,
) -> None:
    """A cache entry with a live TTL is readable on a non-UTC host."""
    from superset.extensions.metastore_cache import SupersetMetastoreCache

    cache = SupersetMetastoreCache(namespace=uuid4(), codec=CODEC)
    assert cache.set("key", VALUE, timeout=300) is True
    assert cache.get("key") == VALUE


def test_metastore_cache_expires_past_ttl(
    non_utc_timezone: str,
    clean_key_value_store: None,
) -> None:
    """A cache entry whose expiry has passed is not returned."""
    from superset.daos.key_value import KeyValueDAO
    from superset.extensions.metastore_cache import SupersetMetastoreCache

    cache = SupersetMetastoreCache(namespace=uuid4(), codec=CODEC)
    cache.set("key", VALUE, timeout=300)
    KeyValueDAO.upsert_entry(
        resource=RESOURCE,
        key=cache.get_key("key"),
        value=VALUE,
        codec=CODEC,
        expires_on=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.session.commit()  # pylint: disable=consider-using-transaction
    assert cache.get("key") is None


def test_short_lived_entry_is_not_immediately_expired(
    non_utc_timezone: str,
    clean_key_value_store: None,
) -> None:
    """An entry created with a short aware TTL (e.g. PKCE) survives immediately."""
    from superset.daos.key_value import KeyValueDAO

    key = uuid4()
    KeyValueDAO.create_entry(
        resource=RESOURCE,
        value=VALUE,
        codec=CODEC,
        key=key,
        expires_on=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db.session.commit()  # pylint: disable=consider-using-transaction
    assert KeyValueDAO.get_value(RESOURCE, key, CODEC) == VALUE


def test_delete_expired_entries_uses_utc(
    non_utc_timezone: str,
    clean_key_value_store: None,
) -> None:
    """Bulk expiry deletion only removes entries that are actually expired."""
    from superset.daos.key_value import KeyValueDAO

    live_key, expired_key = uuid4(), uuid4()
    KeyValueDAO.create_entry(
        resource=RESOURCE,
        value=VALUE,
        codec=CODEC,
        key=live_key,
        expires_on=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    KeyValueDAO.create_entry(
        resource=RESOURCE,
        value=VALUE,
        codec=CODEC,
        key=expired_key,
        expires_on=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.session.commit()  # pylint: disable=consider-using-transaction

    KeyValueDAO.delete_expired_entries(RESOURCE)
    db.session.commit()  # pylint: disable=consider-using-transaction

    assert KeyValueDAO.get_entry(RESOURCE, live_key) is not None
    assert KeyValueDAO.get_entry(RESOURCE, expired_key) is None


def test_prune_uses_utc(
    non_utc_timezone: str,
    clean_key_value_store: None,
) -> None:
    """Pruning keeps live entries and removes expired ones on a non-UTC host."""
    from superset.key_value.commands.prune import KeyValuePruneCommand
    from superset.key_value.models import KeyValueEntry

    live = KeyValueEntry(
        resource=RESOURCE,
        value=CODEC.encode(VALUE),
        expires_on=utcnow() + timedelta(minutes=5),
    )
    expired = KeyValueEntry(
        resource=RESOURCE,
        value=CODEC.encode(VALUE),
        expires_on=utcnow() - timedelta(minutes=5),
    )
    db.session.add_all([live, expired])
    db.session.commit()  # pylint: disable=consider-using-transaction
    live_id, expired_id = live.id, expired.id

    KeyValuePruneCommand().run()

    remaining_ids = {row.id for row in db.session.query(KeyValueEntry.id).all()}
    assert live_id in remaining_ids
    assert expired_id not in remaining_ids
