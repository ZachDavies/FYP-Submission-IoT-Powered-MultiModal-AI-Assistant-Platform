{ pkgs ? import <nixpkgs> {} }:
let
  py = pkgs.python3;
  pypkgs = pkgs.python3Packages;

  openwakewordLocal = pypkgs.callPackage ./openwakeword/default.nix {};
in
pkgs.mkShell {
  packages = with pkgs; [
    (py.withPackages (ps: with ps; [
      pip
      virtualenv
      speechrecognition
      pyaudio
      paho-mqtt
      flask
      requests
      numpy
      faster-whisper
      kokoro
      soundfile
      openwakewordLocal
    ]))
    pkg-config
    piper-tts
    portaudio
    mosquitto
    stdenv.cc.cc.lib
    zlib
    espeak-ng
    espeak
    flac
  ];

  shellHook = ''
    unset PYTHONPATH
    export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.portaudio pkgs.stdenv.cc.cc.lib pkgs.zlib pkgs.espeak-ng ]}:$LD_LIBRARY_PATH"
    export ESPEAKNG_DATA_PATH="${pkgs.espeak-ng}/lib/espeak-ng-data"
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    export C_INCLUDE_PATH="${pkgs.portaudio}/include:$C_INCLUDE_PATH"
    export LIBRARY_PATH="${pkgs.portaudio}/lib:$LIBRARY_PATH"

    export HF_HOME="$HOME/.cache/huggingface"
    export HUGGINGFACE_HUB_CACHE="$HOME/.cache/huggingface/hub"

    export ATEN_CPU_CAPABILITY=default
    export OMP_NUM_THREADS=1
    export MKL_NUM_THREADS=1

    export KMP_DUPLICATE_LIB_OK=TRUE
    export KMP_INIT_AT_FORK=FALSE

    export HF_HUB_OFFLINE=1 # disable hugging face network access to stop models from trying to get updates on each run
    export HF_HUB_DISABLE_IMPLICIT_TOKEN=1

    if [ -d .venv ] && ! .venv/bin/python --version &>/dev/null; then
      echo "[shell] Stale venv detected (Nix store path changed), recreating..."
      rm -rf .venv
    fi
    
    if [ ! -d .venv ]; then
      echo "[shell] Creating venv for pip overrides..."
      python -m venv .venv --system-site-packages
    fi
    source .venv/bin/activate

    # Upgrade misaki to latest — nixpkgs version is too old for kokoro
    python -c "from misaki.en import DEFAULT_DICT" 2>/dev/null || {
      echo "[shell] Upgrading misaki..."
      pip install -q --upgrade "misaki[en]"
    }

  '';

}

