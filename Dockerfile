FROM python:3.12-slim-bookworm

# install OS dependencies
RUN apt update && apt install -y curl

# install poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
RUN ln -s ~/.local/bin/poetry /bin/poetry

# set up project files
WORKDIR /app
COPY poetry.lock pyproject.toml README.md ./
COPY ./publish_cnm.py /app/publish_cnm.py

# install dependencies
RUN poetry lock
RUN poetry install

# run command
ENTRYPOINT ["poetry", "run", "publish_cnm"]