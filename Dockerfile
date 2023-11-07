FROM python:3.12

LABEL maintainer="Alessio Gerace <a.gerace@inrim.it>"
ARG APP_GROUP
ARG APP_NAME
ARG TZ
ARG REQUIREMENTS


ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV DEBIAN_FRONTEND noninteractive

COPY ./requirements.txt /app/requirements.txt
COPY ./requirements_test.txt /app/requirements_test.txt
COPY ./tests/run_app.sh /run_app.sh
COPY ./. /.

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN apt-get update;  \
    apt-get upgrade;  \
    apt-get install -y \
            build-essential python3-dev git \
            ldap-utils libldap-dev libsasl2-dev python3-dev \
            gcc g++ locales locales-all; \
    apt-get clean

ENV LC_ALL it_IT.UTF-8
ENV LANG en_US.UTF-8
ENV LANGUAGE en_US.UTF-8

RUN chmod +x /run_app.sh
RUN pip install --upgrade pip
RUN pip install --upgrade -e .
RUN pip install -r /app/requirements.txt
RUN pip install -r /app/requirements_test.txt



