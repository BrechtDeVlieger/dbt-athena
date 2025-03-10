import decimal
import os
from unittest import mock
from unittest.mock import patch

import agate
import boto3
import pytest
from moto import mock_athena, mock_glue, mock_s3, mock_sts
from moto.core import DEFAULT_ACCOUNT_ID

from dbt.adapters.athena import AthenaAdapter
from dbt.adapters.athena import Plugin as AthenaPlugin
from dbt.adapters.athena.column import AthenaColumn
from dbt.adapters.athena.connections import AthenaCursor, AthenaParameterFormatter
from dbt.adapters.athena.relation import AthenaRelation, TableType
from dbt.clients import agate_helper
from dbt.contracts.connection import ConnectionState
from dbt.contracts.files import FileHash
from dbt.contracts.graph.nodes import CompiledNode, DependsOn, NodeConfig
from dbt.exceptions import ConnectionError, DbtRuntimeError
from dbt.node_types import NodeType

from .constants import (
    ATHENA_WORKGROUP,
    AWS_REGION,
    BUCKET,
    DATA_CATALOG_NAME,
    DATABASE_NAME,
    S3_STAGING_DIR,
    SHARED_DATA_CATALOG_NAME,
)
from .fixtures import seed_data
from .utils import (
    MockAWSService,
    TestAdapterConversions,
    config_from_parts_or_dicts,
    inject_adapter,
)


class TestAthenaAdapter:
    mock_aws_service = MockAWSService()

    def setup_method(self, _):
        project_cfg = {
            "name": "X",
            "version": "0.1",
            "profile": "test",
            "project-root": "/tmp/dbt/does-not-exist",
            "config-version": 2,
        }
        profile_cfg = {
            "outputs": {
                "test": {
                    "type": "athena",
                    "s3_staging_dir": S3_STAGING_DIR,
                    "region_name": AWS_REGION,
                    "database": DATA_CATALOG_NAME,
                    "work_group": ATHENA_WORKGROUP,
                    "schema": DATABASE_NAME,
                }
            },
            "target": "test",
        }

        self.config = config_from_parts_or_dicts(project_cfg, profile_cfg)
        self._adapter = None
        self.mock_manifest = mock.MagicMock()
        self.mock_manifest.get_used_schemas.return_value = {
            ("awsdatacatalog", "foo"),
            ("awsdatacatalog", "quux"),
            ("awsdatacatalog", "baz"),
            (SHARED_DATA_CATALOG_NAME, "foo"),
        }
        self.mock_manifest.nodes = {
            "model.root.model1": CompiledNode(
                name="model1",
                database="awsdatacatalog",
                schema="foo",
                resource_type=NodeType.Model,
                unique_id="model.root.model1",
                alias="bar",
                fqn=["root", "model1"],
                package_name="root",
                refs=[],
                sources=[],
                depends_on=DependsOn(),
                config=NodeConfig.from_dict(
                    {
                        "enabled": True,
                        "materialized": "table",
                        "persist_docs": {},
                        "post-hook": [],
                        "pre-hook": [],
                        "vars": {},
                        "meta": {"owner": "data-engineers"},
                        "quoting": {},
                        "column_types": {},
                        "tags": [],
                    }
                ),
                tags=[],
                path="model1.sql",
                original_file_path="model1.sql",
                compiled=True,
                extra_ctes_injected=False,
                extra_ctes=[],
                checksum=FileHash.from_contents(""),
                raw_code="select * from source_table",
                language="",
            ),
            "model.root.model2": CompiledNode(
                name="model2",
                database="awsdatacatalog",
                schema="quux",
                resource_type=NodeType.Model,
                unique_id="model.root.model2",
                alias="bar",
                fqn=["root", "model2"],
                package_name="root",
                refs=[],
                sources=[],
                depends_on=DependsOn(),
                config=NodeConfig.from_dict(
                    {
                        "enabled": True,
                        "materialized": "table",
                        "persist_docs": {},
                        "post-hook": [],
                        "pre-hook": [],
                        "vars": {},
                        "meta": {"owner": "data-analysts"},
                        "quoting": {},
                        "column_types": {},
                        "tags": [],
                    }
                ),
                tags=[],
                path="model2.sql",
                original_file_path="model2.sql",
                compiled=True,
                extra_ctes_injected=False,
                extra_ctes=[],
                checksum=FileHash.from_contents(""),
                raw_code="select * from source_table",
                language="",
            ),
            "model.root.model3": CompiledNode(
                name="model2",
                database="awsdatacatalog",
                schema="baz",
                resource_type=NodeType.Model,
                unique_id="model.root.model3",
                alias="qux",
                fqn=["root", "model2"],
                package_name="root",
                refs=[],
                sources=[],
                depends_on=DependsOn(),
                config=NodeConfig.from_dict(
                    {
                        "enabled": True,
                        "materialized": "table",
                        "persist_docs": {},
                        "post-hook": [],
                        "pre-hook": [],
                        "vars": {},
                        "meta": {"owner": "data-engineers"},
                        "quoting": {},
                        "column_types": {},
                        "tags": [],
                    }
                ),
                tags=[],
                path="model3.sql",
                original_file_path="model3.sql",
                compiled=True,
                extra_ctes_injected=False,
                extra_ctes=[],
                checksum=FileHash.from_contents(""),
                raw_code="select * from source_table",
                language="",
            ),
            "model.root.model4": CompiledNode(
                name="model4",
                database=SHARED_DATA_CATALOG_NAME,
                schema="foo",
                resource_type=NodeType.Model,
                unique_id="model.root.model4",
                alias="bar",
                fqn=["root", "model4"],
                package_name="root",
                refs=[],
                sources=[],
                depends_on=DependsOn(),
                config=NodeConfig.from_dict(
                    {
                        "enabled": True,
                        "materialized": "table",
                        "persist_docs": {},
                        "post-hook": [],
                        "pre-hook": [],
                        "vars": {},
                        "meta": {"owner": "data-engineers"},
                        "quoting": {},
                        "column_types": {},
                        "tags": [],
                    }
                ),
                tags=[],
                path="model4.sql",
                original_file_path="model4.sql",
                compiled=True,
                extra_ctes_injected=False,
                extra_ctes=[],
                checksum=FileHash.from_contents(""),
                raw_code="select * from source_table",
                language="",
            ),
        }

    @property
    def adapter(self):
        if self._adapter is None:
            self._adapter = AthenaAdapter(self.config)
            inject_adapter(self._adapter, AthenaPlugin)
        return self._adapter

    @mock.patch("dbt.adapters.athena.connections.AthenaConnection")
    def test_acquire_connection_validations(self, connection_cls):
        try:
            connection = self.adapter.acquire_connection("dummy")
        except DbtRuntimeError as e:
            pytest.fail(f"got ValidationException: {e}")
        except BaseException as e:
            pytest.fail(f"acquiring connection failed with unknown exception: {e}")

        connection_cls.assert_not_called()
        connection.handle
        connection_cls.assert_called_once()
        _, arguments = connection_cls.call_args_list[0]
        assert arguments["s3_staging_dir"] == "s3://my-bucket/test-dbt/"
        assert arguments["endpoint_url"] is None
        assert arguments["schema_name"] == "test_dbt_athena"
        assert arguments["work_group"] == "dbt-athena-adapter"
        assert arguments["cursor_class"] == AthenaCursor
        assert isinstance(arguments["formatter"], AthenaParameterFormatter)
        assert arguments["poll_interval"] == 1.0
        assert arguments["retry_config"].attempt == 5
        assert arguments["retry_config"].exceptions == (
            "ThrottlingException",
            "TooManyRequestsException",
            "InternalServerException",
        )

    @mock.patch("dbt.adapters.athena.connections.AthenaConnection")
    def test_acquire_connection(self, connection_cls):
        connection = self.adapter.acquire_connection("dummy")

        connection_cls.assert_not_called()
        connection.handle
        assert connection.state == ConnectionState.OPEN
        assert connection.handle is not None
        connection_cls.assert_called_once()

    @mock.patch("dbt.adapters.athena.connections.AthenaConnection")
    def test_acquire_connection_exc(self, connection_cls, dbt_error_caplog):
        connection_cls.side_effect = lambda **_: (_ for _ in ()).throw(Exception("foobar"))
        connection = self.adapter.acquire_connection("dummy")
        conn_res = None
        with pytest.raises(ConnectionError) as exc:
            conn_res = connection.handle

        assert conn_res is None
        assert connection.state == ConnectionState.FAIL
        assert exc.value.__str__() == "foobar"
        assert "Got an error when attempting to open a Athena connection due to foobar" in dbt_error_caplog.getvalue()

    @pytest.mark.parametrize(
        ("s3_data_dir", "s3_data_naming", "s3_path_table_part", "external_location", "is_temporary_table", "expected"),
        (
            pytest.param(None, "table", None, None, False, "s3://my-bucket/test-dbt/tables/table", id="table naming"),
            pytest.param(None, "uuid", None, None, False, "s3://my-bucket/test-dbt/tables/uuid", id="uuid naming"),
            pytest.param(
                None,
                "table_unique",
                None,
                None,
                False,
                "s3://my-bucket/test-dbt/tables/table/uuid",
                id="table_unique naming",
            ),
            pytest.param(
                None,
                "schema_table",
                None,
                None,
                False,
                "s3://my-bucket/test-dbt/tables/schema/table",
                id="schema_table naming",
            ),
            pytest.param(
                None,
                "schema_table_unique",
                None,
                None,
                False,
                "s3://my-bucket/test-dbt/tables/schema/table/uuid",
                id="schema_table_unique naming",
            ),
            pytest.param(
                "s3://my-data-bucket/",
                "schema_table_unique",
                None,
                None,
                False,
                "s3://my-data-bucket/schema/table/uuid",
                id="data_dir set",
            ),
            pytest.param(
                "s3://my-data-bucket/",
                "schema_table_unique",
                None,
                "s3://path/to/external/",
                False,
                "s3://path/to/external",
                id="external_location set and not temporary",
            ),
            pytest.param(
                "s3://my-data-bucket/",
                "schema_table_unique",
                None,
                "s3://path/to/external/",
                True,
                "s3://my-data-bucket/schema/table/uuid",
                id="external_location set and temporary",
            ),
            pytest.param(
                None,
                "schema_table_unique",
                "other_table",
                None,
                False,
                "s3://my-bucket/test-dbt/tables/schema/other_table/uuid",
                id="s3_path_table_part set",
            ),
        ),
    )
    @patch("dbt.adapters.athena.impl.uuid4", return_value="uuid")
    def test_s3_table_location(
        self, _, s3_data_dir, s3_data_naming, external_location, s3_path_table_part, is_temporary_table, expected
    ):
        self.adapter.acquire_connection("dummy")
        assert expected == self.adapter.s3_table_location(
            s3_data_dir, s3_data_naming, "schema", "table", s3_path_table_part, external_location, is_temporary_table
        )

    def test_s3_table_location_exc(self):
        self.adapter.acquire_connection("dummy")
        with pytest.raises(ValueError) as exc:
            self.adapter.s3_table_location(None, "other", "schema", "table")
        assert exc.value.__str__() == "Unknown value for s3_data_naming: other"

    @mock_glue
    @mock_s3
    @mock_athena
    def test_get_table_location(self, dbt_debug_caplog):
        table_name = "test_table"
        self.adapter.acquire_connection("dummy")
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table(table_name)
        assert self.adapter.get_table_location(DATABASE_NAME, table_name) == "s3://test-dbt-athena/tables/test_table"

    @mock_glue
    @mock_s3
    @mock_athena
    def test_get_table_location_with_failure(self, dbt_debug_caplog):
        table_name = "test_table"
        self.adapter.acquire_connection("dummy")
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        assert self.adapter.get_table_location(DATABASE_NAME, table_name) is None
        assert f"Table '{table_name}' does not exists - Ignoring" in dbt_debug_caplog.getvalue()

    @pytest.fixture(scope="function")
    def aws_credentials(self):
        """Mocked AWS Credentials for moto."""
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION

    @mock_glue
    @mock_s3
    @mock_athena
    def test_clean_up_partitions_will_work(self, dbt_debug_caplog, aws_credentials):
        table_name = "table"
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table(table_name)
        self.mock_aws_service.add_data_in_table(table_name)
        self.adapter.acquire_connection("dummy")
        self.adapter.clean_up_partitions(DATABASE_NAME, table_name, "dt < '2022-01-03'")
        log_records = dbt_debug_caplog.getvalue()
        assert (
            "Deleting table data: path="
            "'s3://test-dbt-athena/tables/table/dt=2022-01-01', "
            "bucket='test-dbt-athena', "
            "prefix='tables/table/dt=2022-01-01/'" in log_records
        )
        assert (
            "Deleting table data: path="
            "'s3://test-dbt-athena/tables/table/dt=2022-01-02', "
            "bucket='test-dbt-athena', "
            "prefix='tables/table/dt=2022-01-02/'" in log_records
        )
        s3 = boto3.client("s3", region_name=AWS_REGION)
        keys = [obj["Key"] for obj in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
        assert set(keys) == {"tables/table/dt=2022-01-03/data1.parquet", "tables/table/dt=2022-01-03/data2.parquet"}

    @mock_glue
    @mock_athena
    def test_clean_up_table_table_does_not_exist(self, dbt_debug_caplog, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        result = self.adapter.clean_up_table(DATABASE_NAME, "table")
        assert result is None
        assert "Table 'table' does not exists - Ignoring" in dbt_debug_caplog.getvalue()

    @mock_glue
    @mock_athena
    def test_clean_up_table_view(self, dbt_debug_caplog, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        self.mock_aws_service.create_view("test_view")
        result = self.adapter.clean_up_table(DATABASE_NAME, "test_view")
        assert result is None

    @mock_glue
    @mock_s3
    @mock_athena
    def test_clean_up_table_delete_table(self, dbt_debug_caplog, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table("table")
        self.mock_aws_service.add_data_in_table("table")
        self.adapter.acquire_connection("dummy")
        self.adapter.clean_up_table(DATABASE_NAME, "table")
        assert (
            "Deleting table data: path='s3://test-dbt-athena/tables/table', "
            "bucket='test-dbt-athena', "
            "prefix='tables/table/'" in dbt_debug_caplog.getvalue()
        )
        s3 = boto3.client("s3", region_name=AWS_REGION)
        objs = s3.list_objects_v2(Bucket=BUCKET)
        assert objs["KeyCount"] == 0

    @patch("dbt.adapters.athena.impl.SQLAdapter.quote_seed_column")
    def test_quote_seed_column(self, parent_quote_seed_column):
        self.adapter.quote_seed_column("col", None)
        parent_quote_seed_column.assert_called_once_with("col", False)

    @mock_glue
    @mock_athena
    @mock_sts
    def test__get_one_catalog(self):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database("foo")
        self.mock_aws_service.create_database("quux")
        self.mock_aws_service.create_database("baz")
        self.mock_aws_service.create_table(table_name="bar", database_name="foo")
        self.mock_aws_service.create_table(table_name="bar", database_name="quux")
        self.mock_aws_service.create_table_without_type(table_name="qux", database_name="baz")
        mock_information_schema = mock.MagicMock()
        mock_information_schema.path.database = "awsdatacatalog"

        self.adapter.acquire_connection("dummy")
        actual = self.adapter._get_one_catalog(
            mock_information_schema,
            {
                "foo": {"bar"},
                "quux": {"bar"},
                "baz": {"qux"},
            },
            self.mock_manifest,
        )

        expected_column_names = (
            "table_database",
            "table_schema",
            "table_name",
            "table_type",
            "table_comment",
            "column_name",
            "column_index",
            "column_type",
            "column_comment",
            "table_owner",
        )
        expected_rows = [
            ("awsdatacatalog", "foo", "bar", "table", None, "id", 0, "string", None, "data-engineers"),
            ("awsdatacatalog", "foo", "bar", "table", None, "country", 1, "string", None, "data-engineers"),
            ("awsdatacatalog", "foo", "bar", "table", None, "dt", 2, "date", None, "data-engineers"),
            ("awsdatacatalog", "quux", "bar", "table", None, "id", 0, "string", None, "data-analysts"),
            ("awsdatacatalog", "quux", "bar", "table", None, "country", 1, "string", None, "data-analysts"),
            ("awsdatacatalog", "quux", "bar", "table", None, "dt", 2, "date", None, "data-analysts"),
            ("awsdatacatalog", "baz", "qux", "table", None, "id", 0, "string", None, "data-engineers"),
            ("awsdatacatalog", "baz", "qux", "table", None, "country", 1, "string", None, "data-engineers"),
        ]

        assert actual.column_names == expected_column_names
        assert len(actual.rows) == len(expected_rows)
        for row in actual.rows.values():
            assert row.values() in expected_rows

    @mock_glue
    @mock_athena
    def test__get_one_catalog_shared_catalog(self):
        self.mock_aws_service.create_data_catalog(
            catalog_name=SHARED_DATA_CATALOG_NAME, catalog_id=SHARED_DATA_CATALOG_NAME
        )
        self.mock_aws_service.create_database("foo", catalog_id=SHARED_DATA_CATALOG_NAME)
        self.mock_aws_service.create_table(table_name="bar", database_name="foo", catalog_id=SHARED_DATA_CATALOG_NAME)
        mock_information_schema = mock.MagicMock()
        mock_information_schema.path.database = SHARED_DATA_CATALOG_NAME

        self.adapter.acquire_connection("dummy")
        actual = self.adapter._get_one_catalog(
            mock_information_schema,
            {
                "foo": {"bar"},
            },
            self.mock_manifest,
        )

        expected_column_names = (
            "table_database",
            "table_schema",
            "table_name",
            "table_type",
            "table_comment",
            "column_name",
            "column_index",
            "column_type",
            "column_comment",
            "table_owner",
        )
        expected_rows = [
            ("9876543210", "foo", "bar", "table", None, "id", 0, "string", None, "data-engineers"),
            ("9876543210", "foo", "bar", "table", None, "country", 1, "string", None, "data-engineers"),
            ("9876543210", "foo", "bar", "table", None, "dt", 2, "date", None, "data-engineers"),
        ]

        assert actual.column_names == expected_column_names
        assert len(actual.rows) == len(expected_rows)
        for row in actual.rows.values():
            assert row.values() in expected_rows

    def test__get_catalog_schemas(self):
        res = self.adapter._get_catalog_schemas(self.mock_manifest)
        assert len(res.keys()) == 2

        information_schema_0 = list(res.keys())[0]
        assert information_schema_0.name == "INFORMATION_SCHEMA"
        assert information_schema_0.schema is None
        assert information_schema_0.database == "awsdatacatalog"
        relations = list(res.values())[0]
        assert set(relations.keys()) == {"foo", "quux", "baz"}
        assert list(relations.values()) == [{"bar"}, {"bar"}, {"qux"}]

        information_schema_1 = list(res.keys())[1]
        assert information_schema_1.name == "INFORMATION_SCHEMA"
        assert information_schema_1.schema is None
        assert information_schema_1.database == SHARED_DATA_CATALOG_NAME
        relations = list(res.values())[1]
        assert set(relations.keys()) == {"foo"}
        assert list(relations.values()) == [{"bar"}]

    @mock_athena
    @mock_sts
    def test__get_data_catalog(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.adapter.acquire_connection("dummy")
        res = self.adapter._get_data_catalog(DATA_CATALOG_NAME)
        assert {"Name": "awsdatacatalog", "Type": "GLUE", "Parameters": {"catalog-id": DEFAULT_ACCOUNT_ID}} == res

    @mock_glue
    @mock_s3
    @mock_athena
    def test__get_relation_type_table(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table("test_table")
        self.adapter.acquire_connection("dummy")
        table_type = self.adapter.get_table_type(DATABASE_NAME, "test_table")
        assert table_type == TableType.TABLE

    @mock_glue
    @mock_s3
    @mock_athena
    def test__get_relation_type_with_no_type(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table_without_table_type("test_table")
        self.adapter.acquire_connection("dummy")

        with pytest.raises(ValueError):
            self.adapter.get_table_type(DATABASE_NAME, "test_table")

    @mock_glue
    @mock_s3
    @mock_athena
    def test__get_relation_type_view(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_view("test_view")
        self.adapter.acquire_connection("dummy")
        table_type = self.adapter.get_table_type(DATABASE_NAME, "test_view")
        assert table_type == TableType.VIEW

    @mock_glue
    @mock_s3
    @mock_athena
    def test__get_relation_type_iceberg(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_iceberg_table("test_iceberg")
        self.adapter.acquire_connection("dummy")
        table_type = self.adapter.get_table_type(DATABASE_NAME, "test_iceberg")
        assert table_type == TableType.ICEBERG

    def _test_list_relations_without_caching(self, schema_relation):
        self.adapter.acquire_connection("dummy")
        relations = self.adapter.list_relations_without_caching(schema_relation)
        assert len(relations) == 3
        assert all(isinstance(rel, AthenaRelation) for rel in relations)
        relations.sort(key=lambda rel: rel.name)
        other = relations[0]
        table = relations[1]
        view = relations[2]
        assert other.name == "other"
        assert other.type == "table"
        assert table.name == "table"
        assert table.type == "table"
        assert view.name == "view"
        assert view.type == "view"

    @mock_athena
    @mock_glue
    @mock_sts
    def test_list_relations_without_caching_with_awsdatacatalog(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table("table")
        self.mock_aws_service.create_table("other")
        self.mock_aws_service.create_view("view")
        self.mock_aws_service.create_table_without_table_type("without_table_type")
        schema_relation = self.adapter.Relation.create(
            database=DATA_CATALOG_NAME,
            schema=DATABASE_NAME,
            quote_policy=self.adapter.config.quoting,
        )
        self._test_list_relations_without_caching(schema_relation)

    @mock_athena
    @mock_glue
    def test_list_relations_without_caching_with_other_glue_data_catalog(self, aws_credentials):
        data_catalog_name = "other_data_catalog"
        self.mock_aws_service.create_data_catalog(data_catalog_name)
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table("table")
        self.mock_aws_service.create_table("other")
        self.mock_aws_service.create_view("view")
        self.mock_aws_service.create_table_without_table_type("without_table_type")
        schema_relation = self.adapter.Relation.create(
            database=data_catalog_name,
            schema=DATABASE_NAME,
            quote_policy=self.adapter.config.quoting,
        )
        self._test_list_relations_without_caching(schema_relation)

    @mock_athena
    @patch("dbt.adapters.athena.impl.SQLAdapter.list_relations_without_caching", return_value=[])
    def test_list_relations_without_caching_with_non_glue_data_catalog(self, parent_list_relations_without_caching):
        data_catalog_name = "other_data_catalog"
        self.mock_aws_service.create_data_catalog(data_catalog_name, "HIVE")
        schema_relation = self.adapter.Relation.create(
            database=data_catalog_name,
            schema=DATABASE_NAME,
            quote_policy=self.adapter.config.quoting,
        )
        self.adapter.acquire_connection("dummy")
        self.adapter.list_relations_without_caching(schema_relation)
        parent_list_relations_without_caching.assert_called_once_with(schema_relation)

    @pytest.mark.parametrize(
        "s3_path,expected",
        [
            ("s3://my-bucket/test-dbt/tables/schema/table", ("my-bucket", "test-dbt/tables/schema/table/")),
            ("s3://my-bucket/test-dbt/tables/schema/table/", ("my-bucket", "test-dbt/tables/schema/table/")),
        ],
    )
    def test_parse_s3_path(self, s3_path, expected):
        assert self.adapter._parse_s3_path(s3_path) == expected

    @mock_athena
    @mock_glue
    @mock_s3
    def test_swap_table_with_partitions(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        target_table = "target_table"
        source_table = "source_table"
        self.mock_aws_service.create_table(source_table)
        self.mock_aws_service.add_partitions_to_table(DATABASE_NAME, source_table)
        self.mock_aws_service.create_table(target_table)
        self.mock_aws_service.add_partitions_to_table(DATABASE_NAME, source_table)
        self.adapter.swap_table(DATABASE_NAME, source_table, DATABASE_NAME, target_table)
        assert self.adapter.get_table_location(DATABASE_NAME, target_table) == f"s3://{BUCKET}/tables/{source_table}"

    @mock_athena
    @mock_glue
    @mock_s3
    def test_swap_table_without_partitions(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        target_table = "target_table"
        source_table = "source_table"
        self.mock_aws_service.create_table_without_partitions(source_table)
        self.mock_aws_service.create_table_without_partitions(target_table)
        self.adapter.swap_table(DATABASE_NAME, source_table, DATABASE_NAME, target_table)
        assert self.adapter.get_table_location(DATABASE_NAME, target_table) == f"s3://{BUCKET}/tables/{source_table}"

    @mock_athena
    @mock_glue
    @mock_s3
    def test_swap_table_with_partitions_to_one_without(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        target_table = "target_table"
        source_table = "source_table"
        # source table does not have partitions
        self.mock_aws_service.create_table_without_partitions(source_table)

        # the target table has partitions
        self.mock_aws_service.create_table(target_table)
        self.mock_aws_service.add_partitions_to_table(DATABASE_NAME, target_table)

        self.adapter.swap_table(DATABASE_NAME, source_table, DATABASE_NAME, target_table)
        glue_client = boto3.client("glue", region_name=AWS_REGION)

        target_table_partitions = glue_client.get_partitions(DatabaseName=DATABASE_NAME, TableName=target_table).get(
            "Partitions"
        )

        assert self.adapter.get_table_location(DATABASE_NAME, target_table) == f"s3://{BUCKET}/tables/{source_table}"
        assert len(target_table_partitions) == 0

    @mock_athena
    @mock_glue
    @mock_s3
    def test_swap_table_with_no_partitions_to_one_with(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        target_table = "target_table"
        source_table = "source_table"
        self.mock_aws_service.create_table(source_table)
        self.mock_aws_service.add_partitions_to_table(DATABASE_NAME, source_table)
        self.mock_aws_service.create_table_without_partitions(target_table)
        glue_client = boto3.client("glue", region_name=AWS_REGION)
        target_table_partitions = glue_client.get_partitions(DatabaseName=DATABASE_NAME, TableName=target_table).get(
            "Partitions"
        )
        assert len(target_table_partitions) == 0
        self.adapter.swap_table(DATABASE_NAME, source_table, DATABASE_NAME, target_table)
        target_table_partitions_after = glue_client.get_partitions(
            DatabaseName=DATABASE_NAME, TableName=target_table
        ).get("Partitions")

        assert self.adapter.get_table_location(DATABASE_NAME, target_table) == f"s3://{BUCKET}/tables/{source_table}"
        assert len(target_table_partitions_after) == 3

    @mock_athena
    @mock_glue
    def test__get_glue_table_versions_to_expire(self, aws_credentials, dbt_debug_caplog):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        table_name = "my_table"
        self.mock_aws_service.create_table(table_name)
        self.mock_aws_service.add_table_version(DATABASE_NAME, table_name)
        self.mock_aws_service.add_table_version(DATABASE_NAME, table_name)
        self.mock_aws_service.add_table_version(DATABASE_NAME, table_name)
        glue = boto3.client("glue", region_name=AWS_REGION)
        table_versions = glue.get_table_versions(DatabaseName=DATABASE_NAME, TableName=table_name).get("TableVersions")
        assert len(table_versions) == 4
        version_to_keep = 1
        versions_to_expire = self.adapter._get_glue_table_versions_to_expire(DATABASE_NAME, table_name, version_to_keep)
        assert len(versions_to_expire) == 3
        assert [v["VersionId"] for v in versions_to_expire] == ["3", "2", "1"]

    @mock_athena
    @mock_glue
    @mock_s3
    def test_expire_glue_table_versions(self, aws_credentials):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        table_name = "my_table"
        self.mock_aws_service.create_table(table_name)
        self.mock_aws_service.add_table_version(DATABASE_NAME, table_name)
        self.mock_aws_service.add_table_version(DATABASE_NAME, table_name)
        self.mock_aws_service.add_table_version(DATABASE_NAME, table_name)
        glue = boto3.client("glue", region_name=AWS_REGION)
        table_versions = glue.get_table_versions(DatabaseName=DATABASE_NAME, TableName=table_name).get("TableVersions")
        assert len(table_versions) == 4
        version_to_keep = 1
        self.adapter.expire_glue_table_versions(DATABASE_NAME, table_name, version_to_keep, False)
        # TODO delete_table_version is not implemented in moto
        # TODO moto issue https://github.com/getmoto/moto/issues/5952
        # assert len(result) == 3

    @mock_s3
    def test_upload_seed_to_s3(self, aws_credentials):
        seed_table = agate.Table.from_object(seed_data)
        self.adapter.acquire_connection("dummy")

        database = "db_seeds"
        table = "data"

        s3_client = boto3.client("s3", region_name=AWS_REGION)
        s3_client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": AWS_REGION})

        location = self.adapter.upload_seed_to_s3(
            s3_data_dir=f"s3://{BUCKET}",
            s3_data_naming="schema_table",
            external_location=None,
            database_name=database,
            table_name=table,
            table=seed_table,
        )

        prefix = "db_seeds/data"
        objects = s3_client.list_objects(Bucket=BUCKET, Prefix=prefix).get("Contents")

        assert location == f"s3://{BUCKET}/{prefix}"
        assert len(objects) == 1
        assert objects[0].get("Key").endswith(".csv")

    @mock_s3
    def test_upload_seed_to_s3_external_location(self, aws_credentials):
        seed_table = agate.Table.from_object(seed_data)
        self.adapter.acquire_connection("dummy")

        bucket = "my-external-location"
        prefix = "seeds/one"
        external_location = f"s3://{bucket}/{prefix}"

        s3_client = boto3.client("s3", region_name=AWS_REGION)
        s3_client.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": AWS_REGION})

        location = self.adapter.upload_seed_to_s3(
            s3_data_dir=None,
            s3_data_naming="schema_table",
            external_location=external_location,
            database_name="db_seeds",
            table_name="data",
            table=seed_table,
        )

        objects = s3_client.list_objects(Bucket=bucket, Prefix=prefix).get("Contents")

        assert location == f"s3://{bucket}/{prefix}"
        assert len(objects) == 1
        assert objects[0].get("Key").endswith(".csv")

    @mock_athena
    def test_get_work_group_output_location(self, aws_credentials):
        self.adapter.acquire_connection("dummy")
        self.mock_aws_service.create_work_group_with_output_location_enforced(ATHENA_WORKGROUP)
        work_group_location_enforced = self.adapter.is_work_group_output_location_enforced()
        assert work_group_location_enforced

    @mock_athena
    def test_get_work_group_output_location_no_location(self, aws_credentials):
        self.adapter.acquire_connection("dummy")
        self.mock_aws_service.create_work_group_no_output_location(ATHENA_WORKGROUP)
        work_group_location_enforced = self.adapter.is_work_group_output_location_enforced()
        assert not work_group_location_enforced

    @mock_athena
    def test_get_work_group_output_location_not_enforced(self, aws_credentials):
        self.adapter.acquire_connection("dummy")
        self.mock_aws_service.create_work_group_with_output_location_not_enforced(ATHENA_WORKGROUP)
        work_group_location_enforced = self.adapter.is_work_group_output_location_enforced()
        assert not work_group_location_enforced

    @mock_athena
    @mock_glue
    @mock_s3
    def test_persist_docs_to_glue_no_comment(self):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        table_name = "my_table"
        self.mock_aws_service.create_table(table_name)
        schema_relation = self.adapter.Relation.create(
            database=DATA_CATALOG_NAME,
            schema=DATABASE_NAME,
            identifier=table_name,
        )
        self.adapter.persist_docs_to_glue(
            schema_relation,
            {
                "description": """
                        A table with str, 123, &^% \" and '

                          and an other paragraph.
                    """,
                "columns": {
                    "id": {
                        "description": """
                        A column with str, 123, &^% \" and '

                          and an other paragraph.
                    """,
                    }
                },
            },
            False,
            False,
        )
        glue = boto3.client("glue", region_name=AWS_REGION)
        table = glue.get_table(DatabaseName=DATABASE_NAME, Name=table_name).get("Table")
        assert not table.get("Description", "")
        assert not table["Parameters"].get("comment")
        assert all(not col.get("Comment") for col in table["StorageDescriptor"]["Columns"])

    @mock_athena
    @mock_glue
    @mock_s3
    def test_persist_docs_to_glue_comment(self):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        table_name = "my_table"
        self.mock_aws_service.create_table(table_name)
        schema_relation = self.adapter.Relation.create(
            database=DATA_CATALOG_NAME,
            schema=DATABASE_NAME,
            identifier=table_name,
        )
        self.adapter.persist_docs_to_glue(
            schema_relation,
            {
                "description": """
                        A table with str, 123, &^% \" and '

                          and an other paragraph.
                    """,
                "columns": {
                    "id": {
                        "description": """
                        A column with str, 123, &^% \" and '

                          and an other paragraph.
                    """,
                    }
                },
            },
            True,
            True,
        )
        glue = boto3.client("glue", region_name=AWS_REGION)
        table = glue.get_table(DatabaseName=DATABASE_NAME, Name=table_name).get("Table")
        assert table["Description"] == "A table with str, 123, &^% \" and ' and an other paragraph."
        assert table["Parameters"]["comment"] == "A table with str, 123, &^% \" and ' and an other paragraph."
        col_id = [col for col in table["StorageDescriptor"]["Columns"] if col["Name"] == "id"][0]
        assert col_id["Comment"] == "A column with str, 123, &^% \" and ' and an other paragraph."

    @mock_athena
    @mock_glue
    def test_list_schemas(self):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database(name="foo")
        self.mock_aws_service.create_database(name="bar")
        self.mock_aws_service.create_database(name="quux")
        self.adapter.acquire_connection("dummy")
        res = self.adapter.list_schemas("")
        assert sorted(res) == ["bar", "foo", "quux"]

    @mock_athena
    @mock_glue
    def test_get_columns_in_relation(self):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table("tbl_name")
        self.adapter.acquire_connection("dummy")
        columns = self.adapter.get_columns_in_relation(
            self.adapter.Relation.create(
                database=DATA_CATALOG_NAME,
                schema=DATABASE_NAME,
                identifier="tbl_name",
            )
        )
        assert columns == [
            AthenaColumn(column="id", dtype="string", table_type=TableType.TABLE),
            AthenaColumn(column="country", dtype="string", table_type=TableType.TABLE),
            AthenaColumn(column="dt", dtype="date", table_type=TableType.TABLE),
        ]

    @mock_athena
    @mock_glue
    def test_get_columns_in_relation_not_found_table(self):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.adapter.acquire_connection("dummy")
        columns = self.adapter.get_columns_in_relation(
            self.adapter.Relation.create(
                database=DATA_CATALOG_NAME,
                schema=DATABASE_NAME,
                identifier="tbl_name",
            )
        )
        assert columns == []

    @mock_athena
    @mock_glue
    def test_delete_from_glue_catalog(self):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table("tbl_name")
        self.adapter.acquire_connection("dummy")
        relation = self.adapter.Relation.create(database=DATA_CATALOG_NAME, schema=DATABASE_NAME, identifier="tbl_name")
        self.adapter.delete_from_glue_catalog(relation)
        glue = boto3.client("glue", region_name=AWS_REGION)
        tables_list = glue.get_tables(DatabaseName=DATABASE_NAME).get("TableList")
        assert tables_list == []

    @mock_athena
    @mock_glue
    def test_delete_from_glue_catalog_not_found_table(self, dbt_debug_caplog):
        self.mock_aws_service.create_data_catalog()
        self.mock_aws_service.create_database()
        self.mock_aws_service.create_table("tbl_name")
        self.adapter.acquire_connection("dummy")
        relation = self.adapter.Relation.create(
            database=DATA_CATALOG_NAME, schema=DATABASE_NAME, identifier="tbl_does_not_exist"
        )
        delete_table = self.adapter.delete_from_glue_catalog(relation)
        assert delete_table is None
        error_msg = f"Table {relation.render()} does not exist and will not be deleted, ignoring"
        assert error_msg in dbt_debug_caplog.getvalue()

    @pytest.mark.parametrize(
        "response,database,table,columns,lf_tags,expected",
        [
            pytest.param(
                {
                    "Failures": [
                        {
                            "LFTag": {"CatalogId": "test_catalog", "TagKey": "test_key", "TagValues": ["test_values"]},
                            "Error": {"ErrorCode": "test_code", "ErrorMessage": "test_err_msg"},
                        }
                    ]
                },
                "test_database",
                "test_table",
                ["column1", "column2"],
                {"tag_key": "tag_value"},
                None,
                id="lf_tag error",
                marks=pytest.mark.xfail,
            ),
            pytest.param(
                {"Failures": []},
                "test_database",
                None,
                None,
                {"tag_key": "tag_value"},
                "Added LF tags: {'tag_key': 'tag_value'} to test_database",
                id="lf_tag database",
            ),
            pytest.param(
                {"Failures": []},
                "test_db",
                "test_table",
                None,
                {"tag_key": "tag_value"},
                "Added LF tags: {'tag_key': 'tag_value'} to test_db.test_table",
                id="lf_tag database and table",
            ),
            pytest.param(
                {"Failures": []},
                "test_db",
                "test_table",
                ["column1", "column2"],
                {"tag_key": "tag_value"},
                "Added LF tags: {'tag_key': 'tag_value'} to test_db.test_table for columns ['column1', 'column2']",
                id="lf_tag database table and columns",
            ),
        ],
    )
    def test_parse_lf_response(self, response, database, table, columns, lf_tags, expected):
        assert self.adapter.parse_lf_response(response, database, table, columns, lf_tags) == expected

    @pytest.mark.parametrize(
        "lf_tags_columns,expected",
        [
            pytest.param({"tag_key": {"tag_value": ["col1, col2"]}}, True, id="valid lf_tags_columns"),
            pytest.param(None, False, id="empty lf_tags_columns"),
            pytest.param(
                {"tag_key": "tag_value"},
                None,
                id="lf_tags_columns tag config is not a dict",
                marks=pytest.mark.xfail(raises=DbtRuntimeError),
            ),
            pytest.param(
                {"tag_key": {"tag_value": "col1"}},
                None,
                id="lf_tags_columns columns config is not a list",
                marks=pytest.mark.xfail(raises=DbtRuntimeError),
            ),
        ],
    )
    def test_lf_tags_columns_is_valid(self, lf_tags_columns, expected):
        assert self.adapter.lf_tags_columns_is_valid(lf_tags_columns) == expected

    @pytest.mark.parametrize(
        "column,expected",
        [
            pytest.param({"Name": "user_id", "Type": "int", "Parameters": {"iceberg.field.current": "true"}}, True),
            pytest.param({"Name": "user_id", "Type": "int", "Parameters": {"iceberg.field.current": "false"}}, False),
            pytest.param({"Name": "user_id", "Type": "int"}, True),
        ],
    )
    def test__is_current_column(self, column, expected):
        assert self.adapter._is_current_column(column) == expected


class TestAthenaFilterCatalog:
    def test__catalog_filter_table(self):
        manifest = mock.MagicMock()
        manifest.get_used_schemas.return_value = [["a", "B"], ["a", "1234"]]
        column_names = ["table_name", "table_database", "table_schema", "something"]
        rows = [
            ["foo", "a", "b", "1234"],  # include
            ["foo", "a", "1234", "1234"],  # include, w/ table schema as str
            ["foo", "c", "B", "1234"],  # skip
            ["1234", "A", "B", "1234"],  # include, w/ table name as str
        ]
        table = agate.Table(rows, column_names, agate_helper.DEFAULT_TYPE_TESTER)

        result = AthenaAdapter._catalog_filter_table(table, manifest)
        assert len(result) == 3
        for row in result.rows:
            assert isinstance(row["table_schema"], str)
            assert isinstance(row["table_database"], str)
            assert isinstance(row["table_name"], str)
            assert isinstance(row["something"], decimal.Decimal)


class TestAthenaAdapterConversions(TestAdapterConversions):
    def test_convert_text_type(self):
        rows = [
            ["", "a1", "stringval1"],
            ["", "a2", "stringvalasdfasdfasdfa"],
            ["", "a3", "stringval3"],
        ]
        agate_table = self._make_table_of(rows, agate.Text)
        expected = ["string", "string", "string"]
        for col_idx, expect in enumerate(expected):
            assert AthenaAdapter.convert_text_type(agate_table, col_idx) == expect

    def test_convert_number_type(self):
        rows = [
            ["", "23.98", "-1"],
            ["", "12.78", "-2"],
            ["", "79.41", "-3"],
        ]
        agate_table = self._make_table_of(rows, agate.Number)
        expected = ["integer", "double", "integer"]
        for col_idx, expect in enumerate(expected):
            assert AthenaAdapter.convert_number_type(agate_table, col_idx) == expect

    def test_convert_boolean_type(self):
        rows = [
            ["", "false", "true"],
            ["", "false", "false"],
            ["", "false", "true"],
        ]
        agate_table = self._make_table_of(rows, agate.Boolean)
        expected = ["boolean", "boolean", "boolean"]
        for col_idx, expect in enumerate(expected):
            assert AthenaAdapter.convert_boolean_type(agate_table, col_idx) == expect

    def test_convert_datetime_type(self):
        rows = [
            ["", "20190101T01:01:01Z", "2019-01-01 01:01:01"],
            ["", "20190102T01:01:01Z", "2019-01-01 01:01:01"],
            ["", "20190103T01:01:01Z", "2019-01-01 01:01:01"],
        ]
        agate_table = self._make_table_of(rows, [agate.DateTime, agate_helper.ISODateTime, agate.DateTime])
        expected = ["timestamp", "timestamp", "timestamp"]
        for col_idx, expect in enumerate(expected):
            assert AthenaAdapter.convert_datetime_type(agate_table, col_idx) == expect

    def test_convert_date_type(self):
        rows = [
            ["", "2019-01-01", "2019-01-04"],
            ["", "2019-01-02", "2019-01-04"],
            ["", "2019-01-03", "2019-01-04"],
        ]
        agate_table = self._make_table_of(rows, agate.Date)
        expected = ["date", "date", "date"]
        for col_idx, expect in enumerate(expected):
            assert AthenaAdapter.convert_date_type(agate_table, col_idx) == expect
