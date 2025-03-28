#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from builtins import object
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse
from django.utils.encoding import smart_str

from beeswax.conf import LIST_PARTITIONS_LIMIT
from beeswax.models import QueryHistory
from beeswax.server import dbms
from beeswax.test_base import (
  BeeswaxSampleProvider,
  fetch_query_result_data,
  get_query_server_config,
  make_query,
  verify_history,
  wait_for_query_to_finish,
)
from beeswax.views import collapse_whitespace
from desktop.lib.django_test_util import assert_equal_mod_whitespace, make_logged_in_client
from desktop.lib.test_utils import add_permission, grant_access
from hadoop.pseudo_hdfs4 import is_live_cluster
from metastore import parser
from useradmin.models import Group, GroupPermission, HuePermission, User

LOG = logging.getLogger()


def _make_query(client, query, submission_type="Execute",
                udfs=None, settings=None, resources=[],
                wait=False, name=None, desc=None, local=True,
                is_parameterized=True, max=30.0, database='default', email_notify=False, **kwargs):
  """Wrapper around the real make_query"""
  res = make_query(client, query, submission_type,
                   udfs, settings, resources,
                   wait, name, desc, local, is_parameterized, max, database, email_notify, **kwargs)

  # Should be in the history if it's submitted.
  if submission_type == 'Execute':
    fragment = collapse_whitespace(smart_str(query[:20]))
    verify_history(client, fragment=fragment)

  return res


@pytest.mark.django_db
class TestApi():
  def setup_method(self):
    self.client = make_logged_in_client(username="test", groupname="default", recreate=True, is_superuser=False)

    self.user = User.objects.get(username="test")

  def test_show_tables(self):
    grant_access("test", "default", "metastore")
    with patch('beeswax.server.dbms.get') as get:
      get.return_value = Mock(
        get_databases=Mock(
          return_value=['sfdc']
        ),
        get_database=Mock(
          return_value={}
        ),
        get_tables_meta=Mock(
          return_value=[{'name': 'customer'}, {'name': 'opportunities'}]
        ),
        server_name='hive'
      )

      response = self.client.post('/metastore/tables/sfdc?format=json')

      get.assert_called()

    assert response.status_code == 200
    data = json.loads(response.content)
    assert data['status'] == 0
    assert data['table_names'] == ['customer', 'opportunities']
    assert data['tables'] == [{'name': 'customer'}, {'name': 'opportunities'}]

  def test_show_tables_hs2(self):
    grant_access("test", "default", "metastore")
    with patch('beeswax.server.dbms.get') as get:
      with patch('beeswax.server.dbms.KazooClient') as KazooClient:
        server_config = get_query_server_config(name='beeswax')
        KazooClient.return_value = Mock(
          # Bug "TypeError: expected string or buffer" if False, to add a new test case and fix
          exists=Mock(return_value=True),
          get_children=Mock(
            return_value=['serverUri=hive-llap-1.gethue.com:10000;serverUri=hive-llap-2.gethue.com:10000'])
        )
        get.return_value = Mock(
          get_databases=Mock(
            return_value=['sfdc']
          ),
          get_database=Mock(
            return_value={}
          ),
          get_tables_meta=Mock(
            return_value=[{'name': 'customer'}, {'name': 'opportunities'}]
          ),
          server_name='hive'
        )
        response = self.client.post('/metastore/tables/sfdc?format=json')
        get.assert_called()

    assert response.status_code == 200
    data = json.loads(response.content)
    assert data['status'] == 0
    assert data['table_names'] == ['customer', 'opportunities']
    assert data['tables'] == [{'name': 'customer'}, {'name': 'opportunities'}]


@pytest.mark.django_db
@pytest.mark.integration
@pytest.mark.requires_hadoop
class TestMetastoreWithHadoop(BeeswaxSampleProvider):

  def setup_method(self):
    user = User.objects.get(username='test')
    self.db = dbms.get(user, get_query_server_config())

    add_permission("test", "test", "write", "metastore")

  def test_basic_flow(self):
    # Default database should exist
    response = self.client.get("/metastore/databases")
    assert self.db_name in response.context[0]["databases"]

    # Table should have been created
    response = self.client.get("/metastore/tables/")
    assert 200 == response.status_code

    # Switch databases
    response = self.client.get("/metastore/tables/%s?format=json" % self.db_name)
    data = json.loads(response.content)
    assert 'name' in data["tables"][0]
    assert "test" in data["table_names"]

    # Should default to "default" database
    response = self.client.get("/metastore/tables/not_there")
    assert 200 == response.status_code

    # And have detail
    response = self.client.post("/metastore/table/%s/test/?format=json" % self.db_name, {'format': 'json'})
    data = json.loads(response.content)
    assert "foo" in [col['name'] for col in data['cols']]
    assert "SerDe Library:" in [prop['col_name'] for prop in data['properties']], data

    # Remember the number of history items. Use a generic fragment 'test' to pass verification.
    history_cnt = verify_history(self.client, fragment='test')

    # Show table data.
    response = self.client.get("/metastore/table/%s/test/read" % self.db_name, follow=True)
    response = self.client.get(
      reverse("beeswax:api_watch_query_refresh_json", kwargs={'id': response.context[0]['query'].id}), follow=True)
    response = wait_for_query_to_finish(self.client, response, max=30.0)
    # Note that it may not return all rows at once. But we expect at least 10.
    results = fetch_query_result_data(self.client, response)
    assert len(results['results']) > 0
    # This should NOT go into the query history.
    assert verify_history(self.client, fragment='test') == history_cnt, 'Implicit queries should not be saved in the history'

  def test_show_tables(self):
    hql = """
        CREATE TABLE test_show_tables_1 (a int) COMMENT 'Test for show_tables';
        CREATE TABLE test_show_tables_2 (a int) COMMENT 'Test for show_tables';
        CREATE TABLE test_show_tables_3 (a int) COMMENT 'Test for show_tables';
      """
    resp = _make_query(self.client, hql, database=self.db_name)
    resp = wait_for_query_to_finish(self.client, resp, max=30.0)

    # Table should have been created
    response = self.client.get("/metastore/tables/%s?filter=show_tables&format=json" % self.db_name)
    assert 200 == response.status_code
    data = json.loads(response.content)
    assert len(data['tables']) == 3
    assert 'name' in data["tables"][0]
    assert 'comment' in data["tables"][0]
    assert 'type' in data["tables"][0]

    hql = """
        CREATE TABLE test_show_tables_4 (a int) COMMENT 'Test for show_tables';
        CREATE TABLE test_show_tables_5 (a int) COMMENT 'Test for show_tables';
      """
    resp = _make_query(self.client, hql, database=self.db_name)
    resp = wait_for_query_to_finish(self.client, resp, max=30.0)

    # Table should have been created
    response = self.client.get("/metastore/tables/%s?filter=show_tables&format=json" % self.db_name)
    assert 200 == response.status_code
    data = json.loads(response.content)
    assert len(data['tables']) == 5
    assert 'name' in data["tables"][0]
    assert 'comment' in data["tables"][0]
    assert 'type' in data["tables"][0]

    hql = """
        CREATE INDEX test_index ON TABLE test_show_tables_1 (a) AS 'COMPACT' WITH DEFERRED REBUILD;
      """
    resp = _make_query(self.client, hql, wait=True, local=False, max=30.0, database=self.db_name)

    # By default, index table should not appear in show tables view
    response = self.client.get("/metastore/tables/%s?format=json" % self.db_name)
    assert 200 == response.status_code
    data = json.loads(response.content)
    assert 'test_index' not in data['tables']

  def test_describe_view(self):
    resp = self.client.post('/metastore/table/%s/myview' % self.db_name, data={'format': 'json'})
    assert 200 == resp.status_code, resp.content
    data = json.loads(resp.content)
    assert data['is_view']
    assert "myview" == data['name']

  def test_describe_partitions(self):
    response = self.client.post("/metastore/table/%s/test_partitions" % self.db_name, data={'format': 'json'})
    data = json.loads(response.content)
    assert 2 == len(data['partition_keys']), data

    response = self.client.post("/metastore/table/%s/test_partitions/partitions" % self.db_name,
                                data={'format': 'json'}, follow=True)
    data = json.loads(response.content)
    partition_columns = [col for cols in data['partition_values_json'] for col in cols['columns']]
    assert "baz_one" in partition_columns
    assert '12345' in partition_columns, partition_columns
    assert "baz_foo" in partition_columns
    assert '67890' in partition_columns

    # Not partitioned
    response = self.client.get("/metastore/table/%s/test/partitions" % self.db_name, follow=True)
    assert "is not partitioned." in response.content

  def test_describe_partitioned_table_with_limit(self):
    # We have 2 partitions in the test table
    finish = LIST_PARTITIONS_LIMIT.set_for_testing("1")
    try:
      response = self.client.get("/metastore/table/%s/test_partitions/partitions" % self.db_name)
      partition_values_json = json.loads(response.context[0]['partition_values_json'])
      assert 1 == len(partition_values_json)
    finally:
      finish()

    finish = LIST_PARTITIONS_LIMIT.set_for_testing("3")
    try:
      response = self.client.get("/metastore/table/%s/test_partitions/partitions" % self.db_name)
      partition_values_json = json.loads(response.context[0]['partition_values_json'])
      assert 2 == len(partition_values_json)
    finally:
      finish()

  def test_read_partitions(self):
    if not is_live_cluster():
      raise SkipTest

    partition_spec = "baz='baz_one',boom=12345"
    response = self.client.get(
      "/metastore/table/%s/test_partitions/partitions/%s/read" % (self.db_name, partition_spec), follow=True)
    response = self.client.get(
      reverse("beeswax:api_watch_query_refresh_json", kwargs={'id': response.context[0]['query'].id}), follow=True)
    response = wait_for_query_to_finish(self.client, response, max=30.0)
    results = fetch_query_result_data(self.client, response)
    assert len(results['results']) > 0, results

  def test_browse_partition(self):
    partition_spec = "baz='baz_one',boom=12345"
    response = self.client.get(
      "/metastore/table/%s/test_partitions/partitions/%s/browse" % (self.db_name, partition_spec), follow=True)
    if is_live_cluster():
      path = '/user/hive/warehouse/%s.db/test_partitions/baz=baz_one/boom=12345' % self.db_name
    else:
      path = '/user/hive/warehouse/test_partitions/baz=baz_one/boom=12345'
    filebrowser_path = urllib.parse.unquote(reverse("filebrowser:filebrowser.views.view", kwargs={'path': path}))
    assert response.request['PATH_INFO'] == filebrowser_path

  def test_drop_partition(self):
    # Create partition first
    partition_spec = "baz='baz_drop',boom=54321"
    hql = 'ALTER TABLE `%s`.`test_partitions` ADD IF NOT EXISTS PARTITION (%s);' % (self.db_name, partition_spec)
    resp = _make_query(self.client, hql, database=self.db_name)
    wait_for_query_to_finish(self.client, resp, max=30.0)

    # Assert partition exists
    response = self.client.get("/metastore/table/%s/test_partitions/partitions" % self.db_name, {'format': 'json'})
    data = json.loads(response.content)
    assert "baz_drop" in [part['columns'][0] for part in data['partition_values_json']], data

    # Drop partition
    self.client.post(
      "/metastore/table/%s/test_partitions/partitions/drop" % self.db_name,
      {'partition_selection': [partition_spec]},
      follow=True
    )
    query = QueryHistory.objects.latest('id')
    assert_equal_mod_whitespace(
      "ALTER TABLE `%s`.`test_partitions` DROP IF EXISTS PARTITION (%s) PURGE" % (self.db_name, partition_spec),
      query.query
    )
    response = self.client.get("/metastore/table/%s/test_partitions/partitions" % self.db_name, {'format': 'json'})
    data = json.loads(response.content)
    assert "baz_drop" not in [part['columns'][0] for part in data['partition_values_json']], data

  def test_drop_multi_tables(self):
    hql = """
      CREATE TABLE test_drop_1 (a int);
      CREATE TABLE test_drop_2 (a int);
      CREATE TABLE test_drop_3 (a int);
    """
    resp = _make_query(self.client, hql, database=self.db_name)
    resp = wait_for_query_to_finish(self.client, resp, max=30.0)

    # Drop them
    resp = self.client.get('/metastore/tables/drop/%s' % self.db_name, follow=True)
    assert 'want to delete' in resp.content, resp.content
    resp = self.client.post(
      '/metastore/tables/drop/%s' % self.db_name,
      {u'table_selection': [u'test_drop_1', u'test_drop_2', u'test_drop_3'], 'is_embeddable': True}
    )
    assert resp.status_code == 302

  def test_drop_multi_tables_with_skip_trash(self):
    hql = """
      CREATE TABLE test_drop_multi_tables_with_skip_trash_1 (a int);
      CREATE TABLE test_drop_multi_tables_with_skip_trash_2 (a int);
      CREATE TABLE test_drop_multi_tables_with_skip_trash_3 (a int);
    """
    resp = _make_query(self.client, hql, database=self.db_name)
    resp = wait_for_query_to_finish(self.client, resp, max=30.0)

    # Drop them
    resp = self.client.get('/metastore/tables/drop/%s' % self.db_name, follow=True)
    assert 'want to delete' in resp.content, resp.content
    resp = self.client.post(
      '/metastore/tables/drop/%s' % self.db_name,
      {
        u'table_selection': [u'test_drop_multi_tables_with_skip_trash_1',
                             u'test_drop_multi_tables_with_skip_trash_2', u'test_drop_multi_tables_with_skip_trash_3'],
        u'skip_trash': u'on',
        'is_embeddable': True
      }
    )
    assert resp.status_code == 302

    response = self.client.get("/metastore/tables/%s?format=json" % self.db_name)
    assert 200 == response.status_code
    data = json.loads(response.content)
    assert 'test_drop_multi_tables_with_skip_trash_1' not in data['tables']
    assert 'test_drop_multi_tables_with_skip_trash_2' not in data['tables']
    assert 'test_drop_multi_tables_with_skip_trash_3' not in data['tables']

  def test_drop_multi_databases(self):
    db1 = '%s_test_drop_1' % self.db_name
    db2 = '%s_test_drop_2' % self.db_name
    db3 = '%s_test_drop_3' % self.db_name

    try:
      hql = """
        CREATE DATABASE %(db1)s;
        CREATE DATABASE %(db2)s;
        CREATE DATABASE %(db3)s;
      """ % {'db1': db1, 'db2': db2, 'db3': db3}
      resp = _make_query(self.client, hql)
      resp = wait_for_query_to_finish(self.client, resp, max=30.0)

      # Add a table to db1
      hql = "CREATE TABLE " + "`" + db1 + "`." + "`test_drop_1` (a int);"
      resp = _make_query(self.client, hql, database=db1)
      resp = wait_for_query_to_finish(self.client, resp, max=30.0)
      assert resp.status_code == 200

      # Drop them
      resp = self.client.get('/metastore/databases/drop', follow=True)
      assert 'want to delete' in resp.content, resp.content
      resp = self.client.post('/metastore/databases/drop',
                              {u'database_selection': [db1, db2, db3], 'is_embeddable': True})
      assert resp.status_code == 302
    finally:
      make_query(self.client, 'DROP DATABASE IF EXISTS %(db)s' % {'db': db1}, wait=True)
      make_query(self.client, 'DROP DATABASE IF EXISTS %(db)s' % {'db': db2}, wait=True)
      make_query(self.client, 'DROP DATABASE IF EXISTS %(db)s' % {'db': db3}, wait=True)

  def test_load_data(self):
    """
    Test load data queries.
    These require Hadoop, because they ask the metastore
    about whether a table is partitioned.
    """

    # Check that view works
    resp = self.client.get("/metastore/table/%s/test/load" % self.db_name, follow=True)
    assert 'Path' in resp.content

    data_dir = '%(prefix)s/tmp' % {'prefix': self.cluster.fs_prefix}
    data_path = data_dir + '/foo'
    self.cluster.fs.mkdir(data_dir)
    self.cluster.fs.create(data_path, data='123')

    # Try the submission
    response = self.client.post("/metastore/table/%s/test/load" % self.db_name, {'path': data_path, 'overwrite': True},
                                follow=True)
    data = json.loads(response.content)
    query = QueryHistory.objects.get(id=data['query_history_id'])

    assert_equal_mod_whitespace(
      "LOAD DATA INPATH '%(data_path)s' OVERWRITE INTO TABLE `%(db)s`.`test`" % {'data_path': data_path,
                                                                                 'db': self.db_name}, query.query
    )

    resp = self.client.post("/metastore/table/%s/test/load" % self.db_name, {'path': data_path, 'overwrite': False},
                            follow=True)
    query = QueryHistory.objects.latest('id')
    assert_equal_mod_whitespace(
      "LOAD DATA INPATH '%(data_path)s' INTO TABLE `%(db)s`.`test`" % {'data_path': data_path, 'db': self.db_name},
      query.query
    )

    # Try it with partitions
    resp = self.client.post(
      "/metastore/table/%s/test_partitions/load" % self.db_name,
      {'path': data_path, 'partition_0': "alpha", 'partition_1': 12345},
      follow=True
    )
    query = QueryHistory.objects.latest('id')
    assert_equal_mod_whitespace(
      query.query,
      "LOAD DATA INPATH '%(data_path)s' INTO TABLE `%(db)s`.`test_partitions` PARTITION (baz='alpha', boom='12345')" % {
        'data_path': data_path, 'db': self.db_name
      }
    )

  def test_has_write_access_frontend(self):
    client = make_logged_in_client(username='write_access_frontend', groupname='write_access_frontend',
                                   is_superuser=False)
    grant_access("write_access_frontend", "write_access_frontend", "metastore")
    user = User.objects.get(username='write_access_frontend')

    response = client.get("/metastore/databases")
    assert "Drop</button>" not in response.content, response.content
    assert "Create a new database" not in response.content, response.content

    response = client.get("/metastore/tables/")
    assert "Drop</button>" not in response.content, response.content
    assert "Create a new table" not in response.content, response.content

    # Add access
    group, created = Group.objects.get_or_create(name='write_access_frontend')
    perm, created = HuePermission.objects.get_or_create(app='metastore', action='write')
    GroupPermission.objects.get_or_create(group=group, hue_permission=perm)

    response = client.get("/metastore/databases")
    assert "Drop</button>" in response.content, response.content
    assert "Create a new database" in response.content, response.content

    response = client.get("/metastore/tables/")
    assert "Drop</button>" in response.content, response.content
    assert "Create a new table" in response.content, response.content

  def test_has_write_access_backend(self):
    client = make_logged_in_client(username='write_access_backend', groupname='write_access_backend',
                                   is_superuser=False)
    grant_access("write_access_backend", "write_access_backend", "metastore")
    grant_access("write_access_backend", "write_access_backend", "beeswax")
    user = User.objects.get(username='write_access_backend')

    # Only fails if we were using Sentry and won't allow SELECT to user
    resp = _make_query(client, 'CREATE TABLE test_perm_1 (a int);', database=self.db_name)
    resp = wait_for_query_to_finish(client, resp, max=30.0)

    def check(client, http_codes):
      resp = client.get('/metastore/tables/drop/%s' % self.db_name)
      assert resp.status_code in http_codes, resp.content

      resp = client.post('/metastore/tables/drop/%s' % self.db_name, {u'table_selection': [u'test_perm_1']})
      assert resp.status_code in http_codes, resp.content

    check(client, [301])  # Denied

    # Add access
    group, created = Group.objects.get_or_create(name='write_access_backend')
    perm, created = HuePermission.objects.get_or_create(app='metastore', action='write')
    GroupPermission.objects.get_or_create(group=group, hue_permission=perm)

    check(client, [200, 302])  # Ok

  def test_alter_database(self):
    resp = self.client.post(reverse("metastore:get_database_metadata", kwargs={'database': self.db_name}))
    json_resp = json.loads(resp.content)
    assert 'data' in json_resp, json_resp
    assert 'parameters' in json_resp['data'], json_resp
    assert 'message=After Alter' not in json_resp['data']['parameters'], json_resp

    # Alter message
    resp = self.client.post(reverse("metastore:alter_database", kwargs={'database': self.db_name}),
                            {'properties': json.dumps({'message': 'After Alter'})})
    json_resp = json.loads(resp.content)
    assert 0 == json_resp['status'], json_resp
    assert '{message=After Alter}' == json_resp['data']['parameters'], json_resp

  def test_alter_table(self):
    resp = _make_query(self.client, "CREATE TABLE test_alter_table (a int) COMMENT 'Before Alter';",
                       database=self.db_name)
    resp = wait_for_query_to_finish(self.client, resp, max=30.0)

    resp = self.client.get('/metastore/table/%s/test_alter_table' % self.db_name)
    assert 'test_alter_table', resp.content
    assert 'Before Alter', resp.content

    # Alter name
    resp = self.client.post(reverse("metastore:alter_table",
                                    kwargs={'database': self.db_name, 'table': 'test_alter_table'}),
                            {'new_table_name': 'table_altered'})
    json_resp = json.loads(resp.content)
    assert 'table_altered' == json_resp['data']['name'], json_resp

    # Alter comment
    resp = self.client.post(reverse("metastore:alter_table",
                                    kwargs={'database': self.db_name, 'table': 'table_altered'}),
                            {'comment': 'After Alter'})
    json_resp = json.loads(resp.content)
    assert 'After Alter' == json_resp['data']['comment'], json_resp

    # Invalid table name returns error response
    resp = self.client.post(reverse("metastore:alter_table",
                                    kwargs={'database': self.db_name, 'table': 'table_altered'}),
                            {'new_table_name': 'bad name'})
    json_resp = json.loads(resp.content)
    assert 1 == json_resp['status'], json_resp
    assert 'Failed to alter table' in json_resp['data'], json_resp

  def test_alter_column(self):
    resp = _make_query(self.client, 'CREATE TABLE test_alter_column (before_alter int);', database=self.db_name)
    resp = wait_for_query_to_finish(self.client, resp, max=30.0)

    resp = self.client.get('/metastore/table/%s/test_alter_column' % self.db_name)
    assert 'before_alter', resp.content
    assert 'int', resp.content

    # Alter name, type and comment
    resp = self.client.post(
      reverse("metastore:alter_column", kwargs={'database': self.db_name, 'table': 'test_alter_column'}),
      {'column': 'before_alter', 'new_column_name': 'after_alter', 'new_column_type': 'string',
       'comment': 'alter comment'}
    )
    json_resp = json.loads(resp.content)
    assert 'after_alter' == json_resp['data']['name'], json_resp
    assert 'string' == json_resp['data']['type'], json_resp
    assert 'alter comment' == json_resp['data']['comment'], json_resp

    # Invalid column type returns error response
    resp = self.client.post(reverse("metastore:alter_column",
                                    kwargs={'database': self.db_name, 'table': 'test_alter_column'}),
                            {'column': 'before_alter', 'new_column_name': 'foo'})
    json_resp = json.loads(resp.content)
    assert 1 == json_resp['status'], json_resp
    assert 'Failed to alter column' in json_resp['message'], json_resp


class TestParser(object):
  def test_parse_simple(self):
    name = 'simple'
    type = 'string'
    comment = 'test_parse_simple'
    column = {'name': name, 'type': type, 'comment': comment}
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column

  def test_parse_varchar(self):
    name = 'varchar'
    type = 'varchar(1000)'
    comment = 'test_parse_varchar'
    column = {'name': name, 'type': type, 'comment': comment}
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column

  def test_parse_decimal(self):
    name = 'simple'
    type = 'decimal(12,2)'
    comment = 'test_parse_decimal'
    column = {'name': name, 'type': type, 'comment': comment}
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column

  def test_parse_array(self):
    name = 'array'
    type = 'array<string>'
    comment = 'test_parse_array'
    column = {'name': name, 'type': 'array', 'comment': comment, 'item': {'type': 'string'}}
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column

  def test_parse_map(self):
    name = 'map'
    type = 'map<string,int>'
    comment = 'test_parse_map'
    column = {'name': name, 'type': 'map', 'comment': comment, 'key': {'type': 'string'}, 'value': {'type': 'int'}}
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column

  def test_parse_struct(self):
    name = 'struct'
    type = 'struct<name:string,age:int>'
    comment = 'test_parse_struct'
    column = {
      'name': name, 'type': 'struct', 'comment': comment,
      'fields': [{'name': 'name', 'type': 'string'}, {'name': 'age', 'type': 'int'}]
    }
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column

  def test_parse_nested(self):
    name = 'nested'
    type = 'array<struct<name:string,age:int>>'
    comment = 'test_parse_nested'
    column = {
      'name': name, 'type': 'array', 'comment': comment,
      'item': {'type': 'struct', 'fields': [{'name': 'name', 'type': 'string'}, {'name': 'age', 'type': 'int'}]}
    }
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column

  def test_parse_nested_with_array(self):
    name = 'nested'
    type = ('struct<fieldname1:bigint,fieldname2:int,fieldname3:int,fieldname4:array<bigint>,'
            'fieldname5:bigint,fieldname6:array<struct<array_elem:string>>,fieldname7:string>')
    comment = 'test_parse_nested'
    column = {
      'comment': 'test_parse_nested',
      'fields': [
        {'type': 'bigint', 'name': 'fieldname1'},
        {'type': 'int', 'name': 'fieldname2'},
        {'type': 'int', 'name': 'fieldname3'},
        {'item': {'type': 'bigint'}, 'type': 'array', 'name': 'fieldname4'},
        {'type': 'bigint', 'name': 'fieldname5'},
        {'item': {'fields': [{'type': 'string', 'name': 'array_elem'}], 'type': 'struct'}, 'type': 'array',
         'name': 'fieldname6'},
        {'type': 'string', 'name': 'fieldname7'}
      ],
      'type': 'struct', 'name': 'nested'
    }
    parse_tree = parser.parse_column(name, type, comment)
    assert parse_tree == column
