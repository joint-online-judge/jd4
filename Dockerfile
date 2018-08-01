FROM ubuntu:18.04
COPY . /tmp/jd4
COPY ./sources.list /etc/apt/

# Install the basic build essentials
RUN apt-get update && apt-get install -y binutils build-essential cmake

# Install the supported languages
RUN apt-get install -y \
            gcc \
            clang \
            python3 \
            python3-venv \
            python3-dev \
            g++ \
            python

#            fp-compiler \
#            openjdk-8-jdk-headless \
#            php7.0-cli \
#            rustc \
#            ghc \
#            libjavascriptcoregtk-4.0-bin \
#            golang \
#            ruby \
#            mono-runtime \
#            mono-mcs

# Install OpenGL for VG101
RUN apt-get install -y libgl1-mesa-dev libglu1-mesa-dev freeglut3-dev

# Install the python dependencies
RUN python3 -m venv /venv && \
    bash -c "source /venv/bin/activate && \
             pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/jd4/requirements.txt && \
             pip install /tmp/jd4" && \
    apt-get remove -y python3-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /root/.config/jd4 && \
    cp /tmp/jd4/examples/langs.yaml /root/.config/jd4/langs.yaml && \
    rm -rf /tmp/jd4

# Start the server
CMD bash -c "source /venv/bin/activate && \
             python3 -m jd4.daemon"
