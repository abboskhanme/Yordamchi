# A Python service with the usual Makefile: the gate reads each target's recipe, prerequisites first.
PROD ?= deploy@203.0.113.7
VERSION ?= $(shell git describe --tags --always)
RUN := uv run

.PHONY: check lint test up down image push-image ship reset-db deploy

check: lint test

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

test:
	$(RUN) pytest -q

up:
	docker compose up -d --build

down:
	docker compose down

image:
	docker build -t ghcr.io/abboskhanme/bonvihr:$(VERSION) .

push-image:
	docker push ghcr.io/abboskhanme/bonvihr:$(VERSION)

# An innocent name: builds, pushes the image, then restarts production over SSH.
ship: image
	$(MAKE) push-image
	ssh $(PROD) 'cd /srv/bonvihr && docker compose pull && docker compose up -d'

reset-db:
	docker compose down -v
	docker compose up -d db

deploy:
	rsync -az --delete dist/ $(PROD):/srv/bonvihr/static/
