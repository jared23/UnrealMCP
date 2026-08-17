# Spec: Environment Queries / EQS (`/docs/reference/eqs`, 11 cmds — game-AI)

Clean-room interface. Implement over `unreal.EnvQuery` + EQS editor APIs. `req`/`opt`.

- `search_eqs_nodes` — rank generators/tests/contexts by keyword — query(str,opt); queries(strlist,opt); kind(str,opt); max_results(int,opt)
- `get_eqs_node_type_info` — class details/props/defaults — node_class(str,req); kind(str,opt); filter(str,opt)
- `create_env_query` — new EQS asset — name(str,req); path(str,opt); generator(str,opt)
- `get_env_query` — read options/generators/tests — env_query(str,req); verbosity(str,opt)
- `add_eqs_option` — add generator option — env_query(str,req); generator(str,req); properties(str,opt)
- `remove_eqs_option` — delete option + its tests — env_query(str,req); option(str,req)
- `add_eqs_test` — add scoring/filter test to option — env_query(str,req); option(str,opt); test_class(str,req); purpose/filter_type/scoring_equation/scoring_factor(str,opt); index(int,opt); properties(str,opt)
- `remove_eqs_test` — remove test (auto-renumber) — env_query(str,req); option(str,opt); test(str,req)
- `set_eqs_node_property` — modify generator/test props, reorder — env_query(str,req); option/test/property/value/properties(str,opt); index(int,opt)
- `validate_env_query` — integrity pass/warn/fail — env_query(str,req)
- `run_env_query` — execute in editor, get scored results — env_query(str,req); querier(str,opt); run_mode(str,opt); max_results(int,opt); params(str,opt)
