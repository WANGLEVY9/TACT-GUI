.PHONY: test fixture

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

fixture:
	PYTHONPATH=src python3 -m budgetmas.cli team-fixture --out results/team-$$(date +%Y%m%d-%H%M%S)
