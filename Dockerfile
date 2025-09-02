FROM python:3.11-alpine3.22 AS make_requirements_txt

ENV SRC_DIR="/reddit2telegram"
ENV POETRY_HOME=/opt/poetry
ENV POETRY_CACHE_DIR=/opt/.cache
ENV PATH="${PATH}:${POETRY_HOME}/bin"

WORKDIR "${SRC_DIR}-tmp"

RUN python -m venv $POETRY_HOME && $POETRY_HOME/bin/pip install -U pip setuptools && $POETRY_HOME/bin/pip install poetry
RUN $POETRY_HOME/bin/pip install poetry-plugin-export

COPY poetry.lock poetry.lock
COPY pyproject.toml pyproject.toml

RUN poetry export --format=requirements.txt -o requirements.txt

FROM python:3.11-alpine3.22

ENV SRC_DIR="/reddit2telegram"

WORKDIR "${SRC_DIR}"

RUN wget -O /usr/local/bin/dumb-init https://github.com/Yelp/dumb-init/releases/download/v1.2.5/dumb-init_1.2.5_x86_64
RUN chmod +x /usr/local/bin/dumb-init

COPY --from=make_requirements_txt "${SRC_DIR}-tmp/requirements.txt" requirements.txt

RUN apk update && apk add --no-cache bash && \
    python -m venv $SRC_DIR/venv && $SRC_DIR/venv/bin/pip install -r requirements.txt && \
    rm -rf /root/.cache

COPY . .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/usr/local/bin/dumb-init", "--"]
CMD ["${SRC_DIR}/entrypoint.sh"]