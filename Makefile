PACK ?= one_user_one_agent_pack
SEED ?= oua-practice
export PYTHONPATH := src

replay:   ## the one command: replay, simulate, judge, measure, write sample_run/
	python3 -m oua replay --pack $(PACK) --out sample_run --seed $(SEED)

test:
	python3 -m pytest tests -q
