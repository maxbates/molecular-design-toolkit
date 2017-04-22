FROM python:2.7-slim
RUN apt-get -y update \
 && apt-get -y install build-essential python-dev

# Installing docker CLI
RUN apt-get install -y \
     apt-transport-https \
     ca-certificates \
     curl \
     gnupg2 \
     software-properties-common \
 && curl -fsSL https://download.docker.com/linux/debian/gpg | apt-key add - \
 && add-apt-repository \
   "deb [arch=amd64] https://download.docker.com/linux/debian \
   $(lsb_release -cs) \
   stable" \
 && apt-get update \
 && apt-get install -y docker-ce
RUN pip install pytest-cov pytest-xdist python-coveralls

ADD requirements.txt ./mdtreqs.txt
ADD DockerMakefiles/requirements.txt ./dmkreqs.txt
RUN pip install -r mdtreqs.txt
RUN pip install -r dmkreqs.txt

ADD . /opt/molecular-design-toolkit
RUN pip install /opt/molecular-design-toolkit


