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

from unittest.mock import MagicMock, patch

import pytest

from superset.mcp_service.chart.schemas import ColumnRef
from superset.mcp_service.chart.validation.dataset_validator import DatasetValidator
from superset.mcp_service.common.error_schemas import (
    ChartGenerationError,
    DatasetContext,
)


def _validate_sum(sql_type: str) -> list[ChartGenerationError]:
    context = DatasetContext(
        id=69,
        table_name="virtual_metrics",
        schema=None,
        database_name="database",
        available_columns=[
            {"name": "computed_total", "type": sql_type, "is_numeric": False}
        ],
        available_metrics=[],
    )

    return DatasetValidator._validate_aggregations(
        [ColumnRef(name="computed_total", aggregate="SUM")], context
    )


@pytest.mark.parametrize(
    "sql_type",
    [
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "REAL",
        "NUMBER",
        "DOUBLE PRECISION",
        "INT8",
        "FLOAT8",
        "DECIMAL(10, 2)",
        "MONEY",
        "SMALLMONEY",
    ],
)
def test_numeric_type_spelling_is_accepted(sql_type: str) -> None:
    assert _validate_sum(sql_type) == []


@pytest.mark.parametrize("sql_type", ["", "UNKNOWN"])
def test_unknown_type_is_deferred_to_compile_check(sql_type: str) -> None:
    assert _validate_sum(sql_type) == []


@pytest.mark.parametrize("sql_type", ["VARCHAR", "INTERVAL", "POINT"])
def test_non_numeric_type_is_rejected_for_numeric_aggregation(
    sql_type: str,
) -> None:
    assert _validate_sum(sql_type)[0].error_type == "invalid_aggregation"


def _mock_dataset() -> MagicMock:
    column = MagicMock()
    column.column_name = "secret_column"
    column.type = "VARCHAR"
    metric = MagicMock()
    metric.metric_name = "secret_metric"
    dataset = MagicMock()
    dataset.id = 42
    dataset.table_name = "private_table"
    dataset.schema = None
    dataset.database.database_name = "database"
    dataset.columns = [column]
    dataset.metrics = [metric]
    return dataset


@pytest.mark.parametrize("dataset_id", [42, "42", "0b3e6f1e-uuid"])
def test_get_dataset_context_denies_inaccessible_dataset(
    dataset_id: int | str,
) -> None:
    dataset = _mock_dataset()
    with (
        patch("superset.daos.dataset.DatasetDAO.find_by_id", return_value=dataset),
        patch(
            "superset.mcp_service.auth.has_dataset_access", return_value=False
        ) as access,
    ):
        assert DatasetValidator._get_dataset_context(dataset_id) is None
    access.assert_called_once_with(dataset)


def test_get_dataset_context_allows_accessible_dataset() -> None:
    dataset = _mock_dataset()
    with (
        patch("superset.daos.dataset.DatasetDAO.find_by_id", return_value=dataset),
        patch("superset.mcp_service.auth.has_dataset_access", return_value=True),
    ):
        context = DatasetValidator._get_dataset_context(42)
    assert context is not None
    assert [c["name"] for c in context.available_columns] == ["secret_column"]


def test_validate_against_dataset_hides_schema_when_access_denied() -> None:
    from superset.mcp_service.chart.schemas import TableChartConfig

    config = TableChartConfig(
        chart_type="table",
        columns=[ColumnRef(name="guess_metric", saved_metric=True)],
    )
    with (
        patch(
            "superset.daos.dataset.DatasetDAO.find_by_id",
            return_value=_mock_dataset(),
        ),
        patch("superset.mcp_service.auth.has_dataset_access", return_value=False),
    ):
        is_valid, error = DatasetValidator.validate_against_dataset(config, 42)
    assert not is_valid
    assert error is not None
    assert "secret_metric" not in error.model_dump_json()
    assert "secret_column" not in error.model_dump_json()
