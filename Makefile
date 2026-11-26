.PHONY: test dbt-example replay record demo
test:
	python -m pytest -q
dbt-example:   ## reference spec -> real dbt project -> dbt build on sample events
	python scripts/dbt_example.py examples/REQ-020.reference.json
replay:        ## agent, from saved responses (no API key)
	python -m spec_agent.evaluate --mode replay
record:        ## agent for real; needs PROOF_ANTHROPIC_API_KEY
	python -m spec_agent.evaluate --mode record
demo: test dbt-example replay
