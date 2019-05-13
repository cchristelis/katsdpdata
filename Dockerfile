FROM sdp-docker-registry.kat.ac.za:5000/docker-base-build 

MAINTAINER Thomas Bennett "tbennett@ska.ac.za"

# Suppress debconf warnings
ENV DEBIAN_FRONTEND noninteractive

# Install some system packages used by multiple images.
USER root
RUN apt-get -y update && apt-get -y install \
    libhdf5-dev  
USER kat

ENV PATH="$PATH_PYTHON3" VIRTUAL_ENV="$VIRTUAL_ENV_PYTHON3" LD_LIBRARY_PATH="/usr/lib"

COPY requirements.txt /tmp/install/requirements.txt
RUN install-requirements.py --default-versions /home/kat/docker-base/base-requirements.txt -r /tmp/install/requirements.txt 

# Install the current package
COPY . /tmp/install/katsdpdata
WORKDIR /tmp/install/katsdpdata
RUN python ./setup.py clean && pip install --no-index .
