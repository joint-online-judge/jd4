# jd4
#
# VERSION               0.1.0

FROM ubuntu:18.04

ENV HOME="/root"

# Install https support
RUN apt-get update && apt-get install -y apt-transport-https ca-certificates apt-utils

# Install the basic build essentials
# COPY ./sources.list /etc/apt/
RUN apt-get update && \
    apt-get install -y \
            binutils \
            build-essential \
            cmake \
            x11-xserver-utils \
            unrar \
            unzip \
            ssh-client \
            wget \
            curl \
            lsb-release \
            software-properties-common \
            gnupg \
            bubblewrap

# Install the supported languages
RUN apt-get install -y \
            gcc \
            python3-dev \
            python3-pip \
            g++ \
            python \
            openjdk-11-jdk-headless \
            octave

#            fp-compiler \
#            php7.0-cli \
#            rustc \
#            ghc \
#            libjavascriptcoregtk-4.0-bin \
#            golang \
#            ruby \
#            mono-runtime \
#            mono-mcs

# Install OpenGL / gmp for VG101/VE475
RUN apt-get install -y libgl1-mesa-dev libglu1-mesa-dev freeglut3-dev libgmp-dev

# Install googletest
RUN apt-get install -y googletest && \
    cd /usr/src/googletest && \
    cmake . && make -j4 && make install

# Install llvm 17
COPY ./register-clang-version.sh /
RUN wget https://apt.llvm.org/llvm.sh && \
    /bin/bash ./llvm.sh 17 && \
    apt-get install -y clang-tidy-17 clang-format-17 clang-tools-17 && \
    rm llvm.sh && \
    /bin/bash ./register-clang-version.sh 17 1 && \
    rm ./register-clang-version.sh

# Install ocaml
RUN bash -c "yes '' | sh <(curl -fsSL https://raw.githubusercontent.com/ocaml/opam/master/shell/install.sh)" && \
    bash -c "opam init --disable-sandboxing --auto-setup -y"
ENV PATH="/root/.opam/default/bin:${PATH}"
ENV OPAM_SWITCH_PREFIX="/root/.opam/default"
ENV CAML_LD_LIBRARY_PATH="/root/.opam/default/lib/stublibs:/root/.opam/default/lib/ocaml/stublibs:/root/.opam/default/lib/ocaml"
ENV OCAML_TOPLEVEL_PATH="/root/.opam/default/lib/toplevel"


COPY ./requirements.txt ./setup.py /srv/jd4/
WORKDIR /srv/jd4
RUN pip3 install -r ./requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple/

COPY ./examples /srv/jd4/examples
COPY ./jd4 /srv/jd4/jd4
COPY examples/langs.yaml $HOME/.config/jd4/langs.yaml

# Install the python dependencies
#RUN python3 -m venv /venv && \
#    bash -c "source /venv/bin/activate && \
#             pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/jd4/requirements.txt && \
#             pip install /tmp/jd4" && \
#    apt-get remove -y python3-dev && \
#    apt-get autoremove -y && \
#    rm -rf /var/lib/apt/lists/* && \
#    mkdir -p /root/.config/jd4 && \
#    cp /tmp/jd4/examples/langs.yaml /root/.config/jd4/langs.yaml && \
#    rm -rf /tmp/jd4

RUN python3 setup.py build_ext --inplace

# support MATLAB in sandbox
RUN mkdir /$HOME/.matlab && mkdir /$HOME/.matlab/R2018a

ENV SERVER_URL="http://127.0.0.1:34765" \
    UNAME="judge" \
    PASSWORD="123456"

# Start the server
CMD python3 -m jd4.daemon \
    --server-url=$SERVER_URL \
    --uname=$UNAME \
    --password=$PASSWORD
