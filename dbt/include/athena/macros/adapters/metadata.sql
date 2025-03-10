{% macro athena__get_catalog(information_schema, schemas) -%}
    {{ return(adapter.get_catalog()) }}
{%- endmacro %}


{% macro athena__list_schemas(database) -%}
  {{ return(adapter.list_schemas()) }}
{% endmacro %}


{% macro athena__list_relations_without_caching(schema_relation) %}
  {{ return(adapter.list_relations_without_caching(schema_relation)) }}
{% endmacro %}
