FROM apache/airflow:2.10.4-python3.12

USER airflow

# Install packages needed by DAGs (agent-authored pipelines use these)
RUN pip install --no-cache-dir nats-py opensearch-py httpx

# Custom entrypoint: init DB, create admin, start webserver + scheduler
COPY --chown=airflow:root docker/airflow-entrypoint.sh /airflow-entrypoint.sh

USER root
RUN chmod +x /airflow-entrypoint.sh
USER airflow

ENTRYPOINT ["/airflow-entrypoint.sh"]
