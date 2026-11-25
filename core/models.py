# core has no models of its own. This module exists so Django sends core's
# post_migrate signal, which applies runtime DB grants (see core.db_roles).
