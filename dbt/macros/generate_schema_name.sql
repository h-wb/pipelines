{# Use the configured schema as-is (health, crossovers, ...) instead of prefixing the target schema #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set group = custom_schema_name | trim if custom_schema_name is not none else target.schema -%}
    {%- if var('miroir_namespace', none) -%}
        {{ var('miroir_namespace') }}_{{ group }}
    {%- else -%}
        {{ group }}
    {%- endif -%}
{%- endmacro %}
