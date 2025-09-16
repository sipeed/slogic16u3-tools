1. python -m venv .venv
2. source .venv/bin/activate
3. pip install -r ota/requirements.txt
4. pip install -r cli/requirements.txt
5. pip install -r pt/requirements.txt
6. `cd cli` and `cmake -Bbuild -GNinja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_BUILD_TYPE=Debug` and `cmake --build build` to generate `./build/slogic_cli`
7. `cd pt` and `python src/gui.py` to use
