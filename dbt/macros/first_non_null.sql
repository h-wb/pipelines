{# First non-null value of `column` within a group, taking lower `priority` first.
   Used to fill fields one source lacks (e.g. PR bodies only in the export). #}
{% macro first_non_null(column, order_by='priority') -%}
    (array_agg({{ column }} order by {{ order_by }}) filter (where {{ column }} is not null))[1]
{%- endmacro %}
