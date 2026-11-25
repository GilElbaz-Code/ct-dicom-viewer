# Shortcuts; every target is a plain docker compose command you can also run directly.
.PHONY: up down reset demo-data smoke logs test

up:          ## build and start everything on http://localhost:8080
	docker compose up --build -d

down:        ## stop containers, keep data
	docker compose down

reset:       ## stop and delete the database and derived data volumes
	docker compose down -v

demo-data:   ## write a synthetic CT series into ./data/synthetic_chest
	docker compose --profile tools run --rm demo-data

smoke:       ## end-to-end check against the running stack
	scripts/smoke_test.sh

logs:
	docker compose logs -f api worker

test:        ## backend tests in the image's test stage (needs a Postgres for integration tests)
	docker build --target test -t ct-viewer-backend:test backend
	docker run --rm ct-viewer-backend:test
