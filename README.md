# Jarvis Voice Assistant Plugin

This is the Jarvis voice assistant plugin for the Iris system. It provides voice interaction capabilities including wake word detection, speech-to-text, text-to-speech, and AI model integration.

## Prerequisites

- Linux-based system (Ubuntu/Debian recommended)
- Python 3.8 or higher
- Git
- Microphone and speakers/headphones
- Internet connection (for model downloads and API calls if using cloud models)
- NVIDIA GPU with CUDA support (recommended for local model inference, but CPU-only mode is available)

## Installation

1. **Clone the repository** (if not already done):
   ```bash
   git clone <repository-url>
   cd igris
   ```

2. **Run the installation script**:
   ```bash
   chmod +x install.sh
   ./install.sh
   ```
   The installation script will:
   - Install required Python packages
   - Download necessary models (if configured for local models)
   - Set up system services for the daemon
   - Configure audio devices

3. **Configuration**:
   - Edit the configuration file in `config/` to set your preferred models, wake word sensitivity, and audio settings.
   - For model selection, see the Model Selector Usage section below.

## Usage

### Starting the Jarvis Assistant

To start the Jarvis voice assistant daemon:

```bash
# From the igris directory
./daemon/jarvis-daemon start
```

Or to run it in the foreground for debugging:

```bash
./daemon/jarvis-daemon debug
```

### Stopping the Jarvis Assistant

```bash
./daemon/jarvis-daemon stop
```

### Restarting

```bash
./daemon/jarvis-daemon restart
```

### Checking Status

```bash
./daemon/jarvis-daemon status
```

## Model Selector Usage

The Jarvis plugin supports multiple AI models for different tasks (language understanding, speech-to-text, text-to-speech). You can switch between models using the model selector.

### Available Model Types

1. **Language Models (LLM)**: For understanding commands and generating responses
2. **Speech-to-Text (STT)**: For converting your voice to text
3. **Text-to-Speech (TTS)**: For converting responses to voice

### Switching Models

You can switch models in two ways:

1. **Through Configuration Files**:
   - Edit `config/models.yaml` to set your preferred models for each type
   - Example:
     ```yaml
     llm:
       active: "llama2-7b-chat"
       options:
         llama2-7b-chat:
           type: "llama.cpp"
           path: "./models/llama2-7b-chat.Q4_K_M.gguf"
         mistral-7b:
           type: "llama.cpp"
           path: "./models/mistral-7b-instruct-v0.2.Q4_K_M.gguf"
     stt:
       active: "whisper-base"
       # ... similar structure
     tts:
       active: "coqui-tts"
       # ... similar structure
     ```
   - After changing the configuration, restart the daemon for changes to take effect.

2. **Through Voice Commands** (if enabled):
   - Say "Jarvis, switch to [model name]" to change the active model for a specific task
   - Example: "Jarvis, switch to mistral-7b for language model"
   - The system will confirm the switch and reload the model.

### Adding Custom Models

1. Place your model files in the `models/` directory
2. Add an entry to the appropriate section in `config/models.yaml`
3. Restart the daemon

## Troubleshooting

- **No audio output**: Check your audio settings in `config/audio.yaml` and ensure your speakers are not muted.
- **Wake word not detected**: Adjust the sensitivity in `config/wake_word.yaml` or check your microphone input.
- **Model loading errors**: Ensure you have enough RAM/VRAM and that the model files are correctly placed and formatted.
- **High CPU usage**: Consider using a smaller model or enabling GPU acceleration if available.

## Support

For issues and feature requests, please visit the project's issue tracker.

Enjoy your Jarvis voice assistant!
