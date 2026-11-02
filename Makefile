.PHONY: install run demo test lint docker

install:
	pip install -e ".[dev]"

run:
	python -m salesdesk

demo:  ## fake data + dashboard, no bot token needed
	python -m salesdesk.seed && python -m salesdesk.web --demo

test:
	pytest -q

lint:
	ruff check .

docker:
	docker compose up -d --build
