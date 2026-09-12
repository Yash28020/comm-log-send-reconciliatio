.PHONY: query validate test all

query:
	sqlite3 data/comm_log.db < query.sql

validate:
	python3 validate.py

test:
	python3 -m pytest tests/ -v

all: query validate test
