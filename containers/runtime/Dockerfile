FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install operating system packages

RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates software-properties-common python3 python3-pip python3-venv build-essential libcurl4-openssl-dev libssl-dev libxml2-dev unzip && rm -rf /var/lib/apt/lists/*

# Configure CRAN repository

RUN curl -fsSL https://cloud.r-project.org/bin/linux/ubuntu/marutter_pubkey.asc | gpg --dearmor -o /usr/share/keyrings/cran.gpg

RUN echo "deb [signed-by=/usr/share/keyrings/cran.gpg] https://cloud.r-project.org/bin/linux/ubuntu noble-cran40/" > /etc/apt/sources.list.d/cran-r.list

# Install R

RUN apt-get update && apt-get install -y --no-install-recommends r-base r-base-dev

# Install Google Cloud SDK

RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/google-cloud.gpg

RUN echo "deb [signed-by=/usr/share/keyrings/google-cloud.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list

RUN apt-get update && apt-get install -y --no-install-recommends google-cloud-cli

# Configure Python environment

RUN python3 -m venv /opt/pyenv

ENV PATH="/opt/pyenv/bin:$PATH"

COPY build/python-dependencies.txt /tmp/python-dependencies.txt

RUN test -f /tmp/python-dependencies.txt && echo "Python dependency file found"

RUN pip install --upgrade pip

RUN pip install --no-cache-dir -r /tmp/python-dependencies.txt

# Install R dependencies

COPY build/r-dependencies.txt /tmp/r-dependencies.txt

RUN test -f /tmp/r-dependencies.txt && echo "R dependency file found"

RUN Rscript -e "pkgs <- readLines('/tmp/r-dependencies.txt'); cat('Installing R packages\n'); print(pkgs); install.packages(pkgs, repos='https://cloud.r-project.org')"

# Runtime code

COPY runtime/pipeline_runner.py /app/pipeline_runner.py

COPY runtime/pipeline_executor.py /app/pipeline_executor.py

WORKDIR /app

ENTRYPOINT ["/opt/pyenv/bin/python","/app/pipeline_runner.py"]