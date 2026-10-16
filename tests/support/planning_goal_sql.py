"""Small PostgREST-shaped transport for isolated PostgreSQL acceptance/preview.

Only translates transport calls; business RPCs, triggers and constraints are real.
"""
from types import SimpleNamespace


class GoalSqlQuery:
    def __init__(self, client, table):
        self.client, self.table = client, table
        self.conditions, self.params, self.orders = [], [], []
        self.operation, self.payload, self.maximum = 'select', None, None
        self.fields = '*'

    def select(self, fields='*'):
        self.fields = fields
        return self

    def _where(self, field, operator, value):
        from psycopg import sql
        self.conditions.append(sql.SQL('{} ' + operator + ' %s').format(sql.Identifier(field)))
        self.params.append(value)
        return self

    def eq(self, field, value): return self._where(field, '=', value)
    def neq(self, field, value): return self._where(field, '<>', value)
    def gte(self, field, value): return self._where(field, '>=', value)
    def lte(self, field, value): return self._where(field, '<=', value)
    def lt(self, field, value): return self._where(field, '<', value)
    def gt(self, field, value): return self._where(field, '>', value)

    def in_(self, field, values):
        from psycopg import sql
        self.conditions.append(sql.SQL('{} = any(%s)').format(sql.Identifier(field)))
        self.params.append(values)
        return self

    def is_(self, field, value):
        from psycopg import sql
        if value is not None:
            raise ValueError('Only SQL NULL is supported')
        self.conditions.append(sql.SQL('{} is null').format(sql.Identifier(field)))
        return self

    def order(self, field, desc=False):
        self.orders.append((field, desc))
        return self

    def limit(self, value):
        self.maximum = value
        return self

    def insert(self, payload):
        self.operation, self.payload = 'insert', payload
        return self

    def update(self, payload):
        self.operation, self.payload = 'update', payload
        return self

    def delete(self):
        self.operation = 'delete'
        return self

    def execute(self):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        table = sql.Identifier('public', self.table)
        where = sql.SQL(' where ') + sql.SQL(' and ').join(self.conditions) if self.conditions else sql.SQL('')
        params = list(self.params)
        if self.operation == 'select':
            fields = sql.SQL('*') if self.fields == '*' else sql.SQL(',').join(
                sql.Identifier(field.strip()) for field in self.fields.split(','))
            query = sql.SQL('select {} from {}{}').format(fields, table, where)
            if self.orders:
                query += sql.SQL(' order by ') + sql.SQL(',').join(
                    sql.SQL('{} ' + ('desc' if desc else 'asc')).format(sql.Identifier(key)) for key, desc in self.orders)
            if self.maximum is not None:
                query += sql.SQL(' limit %s')
                params.append(self.maximum)
        elif self.operation == 'delete':
            query = sql.SQL('delete from {}{} returning *').format(table, where)
        else:
            values = [Jsonb(v) if isinstance(v, dict) else v for v in self.payload.values()]
            if self.operation == 'update':
                assignments = sql.SQL(',').join(sql.SQL('{} = %s').format(sql.Identifier(k)) for k in self.payload)
                query = sql.SQL('update {} set {}{} returning *').format(table, assignments, where)
                params = values + params
            else:
                query = sql.SQL('insert into {} ({}) values ({}) returning *').format(
                    table, sql.SQL(',').join(map(sql.Identifier, self.payload)),
                    sql.SQL(',').join(sql.Placeholder() for _ in values))
                params = values
        wrapped = sql.SQL('select to_jsonb(r) from ({}) r').format(query) if self.operation == 'select' else (
            sql.SQL('with r as ({}) select to_jsonb(r) from r').format(query))
        return SimpleNamespace(data=[row[0] for row in self.client.conn.execute(wrapped, params).fetchall()])


class GoalSqlClient:
    def __init__(self, conn):
        self.conn = conn

    def table(self, table):
        return GoalSqlQuery(self, table)

    def rpc(self, name, params):
        def execute():
            from psycopg import sql
            from psycopg.types.json import Jsonb
            args = sql.SQL(',').join(sql.SQL('{} := %s').format(sql.Identifier(k)) for k in params)
            values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in params.values()]
            result = self.conn.execute(sql.SQL('select public.{}({})').format(sql.Identifier(name), args), values).fetchone()[0]
            return SimpleNamespace(data=result)
        return SimpleNamespace(execute=execute)
