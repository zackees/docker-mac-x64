# Fixed rebuild of etasdemir/osx-container's Dockerfile.
#
# Two changes make it buildable again:
#   * ubuntu:latest -> ubuntu:22.04 and `qemu` -> `qemu-system-x86` + `qemu-utils`.
#     The `qemu` metapackage was dropped after 22.04, so `ubuntu:latest` fails.
#   * `python3` added; upstream runs ./fetch-macOS-v2.py without installing it.
# Also dropped virt-manager / libguestfs-tools / uml-utilities: ~500 MB of build
# deps this image never calls.
#
# NOTE: the prebuilt `etasdemir/osx-container:ventura` image is the tested path.
# See "Verification status" in README.md.
FROM ubuntu:22.04
SHELL ["/bin/bash", "-c"]

ARG SHORTNAME=ventura
ARG SIZE=128G
ENV DISPLAY=:0.0
ENV RAM=8
ENV CORES=2
ENV THREADS=4
ENV DISPLAY_MODE=vnc
ENV EXTRA=

RUN apt-get update && apt-get install -y --no-install-recommends \
      qemu-system-x86 qemu-utils \
      python3 python3-pip \
      dmg2img p7zip-full \
      wget curl ca-certificates git sudo \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m user \
    && tee -a /etc/sudoers <<< "user ALL=(ALL) NOPASSWD:ALL" \
    && usermod -aG kvm user

USER user
WORKDIR /home/user

RUN git clone --depth 1 --recursive https://github.com/kholia/OSX-KVM.git
WORKDIR /home/user/OSX-KVM

# Pulls the recovery image from Apple. No Apple software is baked into this repo.
RUN ./fetch-macOS-v2.py -s "${SHORTNAME}" \
    && dmg2img -i BaseSystem.dmg BaseSystem.img \
    && rm BaseSystem.dmg
RUN qemu-img create -f qcow2 mac_hdd_ng.img "${SIZE}"

COPY --chown=user:user Launch.sh /home/user/OSX-KVM/Launch.sh
COPY --chown=user:user scripts/ /home/user/OSX-KVM/scripts/
RUN chmod +x /home/user/OSX-KVM/Launch.sh

CMD /home/user/OSX-KVM/Launch.sh "${RAM}" "${CORES}" "${THREADS}" "${EXTRA}"
