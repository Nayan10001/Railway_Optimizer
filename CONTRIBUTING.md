# Contributing to KTV-PSA Corridor Freight Scheduler

First off, thank you for considering contributing to the KTV-PSA Corridor Freight Scheduler! It is people like you who make the open-source community such an amazing place to learn, inspire, and create.

Please read through these guidelines to ensure a smooth contribution process.

---

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please report any unacceptable behavior to the project maintainers.

---

## How Can I Contribute?

### 1. Reporting Bugs
* Check the existing issues to see if the bug has already been reported.
* If not, open a new issue using our **Bug Report** template.
* Include a clear description, steps to reproduce, and any relevant error logs or screenshots.

### 2. Proposing Features
* Open a new issue using our **Feature Request** template.
* Provide a clear description of the proposed feature and why it would be beneficial to the scheduler.

### 3. Submitting Pull Requests (PRs)
* Fork the repository and create your branch from `main`.
* Follow our branch naming convention:
  * `feature/your-feature-name`
  * `bugfix/your-bug-name`
  * `docs/improvement-name`
* Ensure your code adheres to our style guidelines and passes all tests.
* Open a PR against the `main` branch with a clear description of your changes.

---

## Development Setup

The project uses a hybrid architecture: a performance-critical **Rust** engine linked to a **Python** optimization pipeline and **Streamlit** front-end.

### Prerequisites
* **Python**: Version 3.8 or higher (3.10+ recommended)
* **Rust**: Toolchain including Cargo and `rustc` (Edition 2024)
* **C++ Compiler**: Necessary for compiling native extensions on some platforms.

### Step-by-Step Installation

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/Nayan10001/Railway_Optimizer.git
   cd Railway_Optimizer
   ```

2. **Set Up Python Virtual Environment**:
   ```bash
   python -m venv .venv
   # Activate on Windows:
   .venv\Scripts\activate
   # Activate on Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Build and Link the Rust Native Extension**:
   We use `Maturin` to compile and install the Rust library (`_native`) directly into our Python virtual environment:
   ```bash
   maturin develop
   ```
   To compile in release mode for maximum performance:
   ```bash
   maturin develop --release
   ```

---

## Testing & Formatting

All code submissions must pass our test suites and conform to the code formatting guidelines.

### Running Python Tests
We use `pytest` for the Python testing suite:
```bash
pytest
```

### Running Rust Smoke Tests
We also have a dedicated smoke test suite for the compiled Rust native functions:
```bash
python tests/test_native_smoke.py
```

### Code Formatting
* **Python**: We follow standard PEP 8. Please run `black` or `ruff` before committing.
* **Rust**: Run `cargo fmt` to format the Rust codebase.
  ```bash
  cargo fmt --all
  ```

Thank you for contributing!
