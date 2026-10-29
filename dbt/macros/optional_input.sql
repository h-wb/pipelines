{# dlt infers schemas from received rows. Absent resources are empty; absent
   optional fields are unknown. Required fields on an existing table must exist.
   Calling source() even during parsing retains the real dependency in dbt's graph. #}
{% macro optional_input(source_name, table_name, columns, required=[]) %}
    {% set raw = source(source_name, table_name) %}
    {% if var('miroir_namespace', none) is none %}
        {# Keep legacy view column types and inferred schemas unchanged. #}
        select * from {{ raw }}
    {% else %}
    {% set ns = namespace(relation=none, available=[]) %}
    {% if execute %}
        {% set ns.relation = adapter.get_relation(database=raw.database, schema=raw.schema, identifier=raw.identifier) %}
        {% if ns.relation is not none %}
            {% for column in adapter.get_columns_in_relation(ns.relation) %}
                {% do ns.available.append(column.name | lower) %}
            {% endfor %}
            {% for name in required %}
                {% if name not in ns.available %}
                    {{ exceptions.raise_compiler_error('Required column ' ~ name ~ ' missing from ' ~ raw) }}
                {% endif %}
            {% endfor %}
        {% endif %}
    {% endif %}
    select
    {% for name, type in columns.items() %}
        {% if type == 'double precision' and name ~ '__v_double' in ns.available %}
            coalesce({{ adapter.quote(name ~ '__v_double') }}::double precision,
                {% if name in ns.available %}{{ adapter.quote(name) }}::double precision{% else %}null::double precision{% endif %})
        {% elif name in ns.available %}{{ adapter.quote(name) }}{% else %}null{% endif %}::{{ type }} as {{ adapter.quote(name) }}{% if not loop.last %},{% endif %}
    {% endfor %}
    {% if ns.relation is not none %}from {{ ns.relation }}{% else %}where false{% endif %}
    {% endif %}
{% endmacro %}
