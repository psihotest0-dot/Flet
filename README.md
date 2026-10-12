# Flet Client for AI Agent

This is a Flet-based desktop client application for an AI agent.

## Local Development Setup

To run the project locally for development, follow these step-by-step instructions:

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd <repository_name>
   ```

2. **Set up a virtual environment (optional but recommended):**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
   ```

3. **Install dependencies:**
   Install the project with development dependencies.
   ```bash
   pip install -e .[dev]
   ```

4. **Run the application:**
   You can start the Flet app by running the main Python file.
   ```bash
   python main.py
   ```
   *(Alternatively, you can run `flet run main.py`)*

## Building the Standalone Application

To compile the project into a standalone executable desktop application (`.exe` for Windows, `.app` for macOS), use the Flet build command.

Run the following command from the root of the project:

```bash
flet build <target_platform>
```

Replace `<target_platform>` with your desired operating system target (e.g., `windows`, `macos`, `linux`, `apk`, `aab`, `ipa`).

For example, to build for macOS:
```bash
flet build macos
```

To build for Windows:
```bash
flet build windows
```

The resulting executable will be placed in the `build/` directory (or similar, depending on the target platform).
