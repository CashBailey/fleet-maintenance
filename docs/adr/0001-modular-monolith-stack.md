# ADR 0001: Django and React modular monolith

- Status: accepted
- Date: 2026-09-03

Use Django 5.2 LTS, Django REST Framework, PostgreSQL 16, and a React/Vite PWA. Django owns authentication, authorization, transactions, migrations, files, and the versioned API. The production Django process serves the Vite build. A second invocation of the same codebase runs database-backed jobs.

This implements the research paper's PostgreSQL modular-monolith direction while keeping one transactional authority. Redis, Kafka, Elasticsearch, microservices, and Kubernetes are omitted until measured load or organizational boundaries require them.

