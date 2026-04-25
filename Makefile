.PHONY: install run lint check fmt clean docker-build docker-up docker-down docker-logs gen-key

install:
	python -m pip install -r requirements.txt

run:
	python -m bot

lint:
	ruff check bot

fmt:
	ruff check bot --fix
	ruff format bot

check:
	ruff check bot
	python -m compileall -q bot

clean:
	rm -rf .ruff_cache .mypy_cache __pycache__ */__pycache__ */*/__pycache__

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f bot

gen-key:
	@python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
