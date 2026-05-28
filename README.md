# System Deployment and Execution

This project implements a local smart-environment control pipeline that integrates speech processing, MQTT-based device orchestration, Home Assistant, Zigbee2MQTT, Matter-enabled devices, and a web-based monitoring interface. The software stack is designed to run within a declarative Nix/NixOS environment so that the supporting infrastructure and application services can be reproduced consistently across deployments.

## Prerequisites

Before starting the application pipeline, ensure that the following services are installed and running:

- Ollama.
- Mosquitto MQTT broker.
- Home Assistant.
- Matter containers.
- Zigbee2MQTT.

The corresponding declarative environment definitions are provided through the project `shell.nix` file and the accompanying NixOS service configuration files.

## MQTT Credential Provisioning

Mosquitto user credentials are provisioned as hashed password files and referenced declaratively from the NixOS Mosquitto configuration. The Python orchestrator must still be supplied in `config.py` with the real plaintext MQTT password, because it uses that value to authenticate as `DVES_USER` at runtime. Zigbee2MQTT likewise requires the matching plaintext MQTT username and password in its MQTT settings, while Home Assistant’s `mqtt_statestream` publishes entity state to `base_topic/domain/entity/state` topics.

Create the hashed password files for the Tasmota and Zigbee2MQTT users as follows:

```bash
sudo mkdir -p /etc/secrets
sudo chmod 700 /etc/secrets
sudo nix-shell -p mosquitto --run 'mosquitto_passwd -H sha512-pbkdf2 -c /etc/secrets/mqtt_tasmota DVES_USER'
sudo nix-shell -p mosquitto --run 'mosquitto_passwd -H sha512-pbkdf2 -c /etc/secrets/mqtt_zigbee zigbee2mqtt'
sudo sed -i 's/^DVES_USER://g' /etc/secrets/mqtt_tasmota
sudo sed -i 's/^zigbee2mqtt://g' /etc/secrets/mqtt_zigbee
```

The application components that publish to or subscribe from MQTT must use the same plaintext credentials that were entered when generating these hashes. In particular, the Home Assistant MQTT statestream integration and any Tasmota-facing application components should authenticate with the `DVES_USER` account, while Zigbee2MQTT should authenticate with the `zigbee2mqtt` account.

Mosquitto user credentials are provisioned as hashed password files and referenced declaratively from the NixOS Mosquitto configuration. The Python orchestrator must still be supplied with the plaintext MQTT password in config.py, because it uses that value to authenticate as DVES_USER at runtime. Zigbee2MQTT likewise requires the matching plaintext MQTT username and password in its MQTT settings, while Home Assistant’s mqtt_statestream publishes entity state to base_topic/domain/entity/state topics.


## Matter and Sonoff Integration

Sonoff Matter devices require prior activation within the Sonoff ecosystem before they can be exposed reliably through Home Assistant. The device should first be added to a Sonoff cloud account and confirmed to be operational in the vendor environment, after which it can be linked into Home Assistant through the Sonoff integration.

To make these device states available to the project’s MQTT-driven orchestration layer, Home Assistant should publish entity updates using the MQTT statestream integration. This integration emits state messages using the topic structure `base_topic/domain/entity/state`, allowing Home Assistant-managed entities to be observed by the same MQTT client infrastructure used for Tasmota and Zigbee2MQTT devices.

## Starting the Full Stack

For standard use, the entire stack can be launched automatically with:

```bash
bash start.sh
```

This is the recommended entry point when the required system services are already available and the goal is to start the application layer with minimal manual intervention.

## Manual and Development Execution

For interactive or development-oriented use, enter the declarative environment with:

```bash
nix-shell
```

Once inside the environment, the dashboard can be started separately by changing into the `dashboard` directory and running:

```bash
cd dashboard
python app.py
```

This launches the monitoring dashboard, which exposes device states, sensor readings, and the audit log on `localhost:8001`.

To start the full orchestration and web interface from the project root, run:

```bash
python ./orchestrator/main.py
```

This launches the complete voice-to-voice orchestration pipeline together with the web interface on `localhost:5000`.

## Topic Conventions

The orchestration layer relies on protocol-specific MQTT topic conventions. Tasmota devices receive commands through `cmnd/...` topics and report device state on `stat/...` topics, while Zigbee2MQTT uses `zigbee2mqtt/<device>` for state publication and `zigbee2mqtt/<device>/set` for control messages.

Home Assistant entities exposed through MQTT statestream follow the topic pattern `base_topic/domain/entity/state`. As a result, the dashboard and orchestration components must subscribe to the exact state topics produced by each subsystem, and all MQTT clients must authenticate successfully before state updates can be consumed.

## Equivalent Setup for Non-NixOS Systems

Although the reference deployment uses NixOS for reproducibility, the pipeline can also be executed on conventional Linux distributions such as Ubuntu or Debian, provided that equivalent runtime dependencies are installed and configured consistently. In this case, the goal is not full declarative reproducibility, but functional parity with the NixOS-based environment.
While the NixOS deployment relies on declarative system services, the non-NixOS variant requires manual provisioning of runtime services and explicit configuration of Home Assistant extensions and plugins. To preserve reproducibility, all configuration files should be version-controlled, but their application is performed outside the NixOS module system.


### Core Services

A non-NixOS installation should provide the following components:

- Python and the project’s Python dependencies.
- Ollama for local language model inference.
- Mosquitto as the MQTT broker.
- Home Assistant.
- Zigbee2MQTT.
- Matter containers or services, where applicable.

Ollama provides an official Linux installation script and can be started with `ollama serve` after installation.[6] Mosquitto is commonly available through standard package repositories on Ubuntu and similar distributions, and can be installed with the system package manager.[7][8] Zigbee2MQTT can be installed on Linux using Node.js, Git, and a dedicated working directory such as `/opt/zigbee2mqtt`, then launched with `npm start` or managed through a custom `systemd` service.

### Example Ubuntu/Debian Provisioning

The following example establishes a practical baseline environment on Debian- or Ubuntu-derived systems:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git mosquitto mosquitto-clients portaudio19-dev espeak-ng flac pkg-config zlib1g-dev
curl -fsSL https://ollama.com/install.sh | sh
```

For Zigbee2MQTT, install Node.js 20 or newer together with its build dependencies, clone the Zigbee2MQTT repository, and install the project dependencies using `npm ci`.


### Secret and MQTT Configuration

The same logical credential model used on NixOS should be preserved. Mosquitto should authenticate broker users with hashed password files, while client applications such as Zigbee2MQTT and the Python orchestration pipeline should use the corresponding plaintext credentials at runtime.[5][9]

For a conventional Linux system, the Mosquitto password file can be created directly in the standard Mosquitto format using `mosquitto_passwd`. The Python pipeline, the Tasmota-side integration, and the Home Assistant MQTT statestream bridge should all use the correct `DVES_USER` credentials for broker access, while Zigbee2MQTT should authenticate with its own `zigbee2mqtt` account.

Mosquitto may use hashed password files on the broker side, but the local Python application (config.py) and Zigbee2MQTT must still be supplied with matching plaintext MQTT credentials in their runtime configuration.

### Service Management

On non-NixOS systems, long-running services should ideally be managed through `systemd`. Mosquitto normally starts automatically after package installation on Debian- and Ubuntu-derived systems, and its status can be verified with `systemctl status mosquitto`.[8] Zigbee2MQTT may likewise be installed as a custom `systemd` unit with `/opt/zigbee2mqtt` as its working directory and `npm start` as its execution command.[9][10]

Ollama can be started manually with:

```bash
ollama serve
```

Once the supporting services are available, the project itself can be launched manually by starting the dashboard and orchestrator components in separate terminals.