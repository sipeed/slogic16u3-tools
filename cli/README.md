```bash

cmake -Bbuild -GNinja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build build
./build/slogic_cli

# forever loop
# C-c to exit

./build/slogic_cli --sr 200 --ch 16 --volt 1600
# Received: 1203765248 bytes, Speed: 400.16 MB/s(400.00 MB/s) is 'valid'

./build/slogic_cli --sr 150 --ch 16 --volt 1600
# Received: 304087040 bytes, Speed: 300.19 MB/s(300.00 MB/s) is 'valid'

./build/slogic_cli --sr 400 --ch 8 --volt 1600
# Received: 1203765248 bytes, Speed: 400.16 MB/s(400.00 MB/s) is 'valid'

./build/slogic_cli --sr 375 --ch 8 --volt 1600
# Received: 1126171916 bytes, Speed: 375.02 MB/s(375.00 MB/s) is 'valid'

./build/slogic_cli --sr 750 --ch 4 --volt 1600
# Received: 1128267776 bytes, Speed: 375.02 MB/s(375.00 MB/s) is 'valid'

./build/slogic_cli --sr 600 --ch 4 --volt 1600
# Received: 1205862400 bytes, Speed: 300.19 MB/s(300.00 MB/s) is 'valid'

./build/slogic_cli --sr 1500 --ch 2 --volt 1600
# Received: 1128267776 bytes, Speed: 375.02 MB/s(375.00 MB/s) is 'valid'

./build/slogic_cli --sr 1200 --ch 2 --volt 1600
# Received: 905969664 bytes, Speed: 299.89 MB/s(300.00 MB/s) is 'valid'

```


```bash
./build/slogic_cli --sr 800 --ch 4 --volt 1600

python show.py 4ch_800M_wave.bin
# Detected: 4 channels, 800000000 Hz sample rate
# Total samples: 16777216
# CH0: PWM freq = 10.073452MHz, duty cycle = 50.16%
# CH1: PWM freq = 50.355330MHz, duty cycle = 53.73%
# CH2: PWM freq = 50.413223MHz, duty cycle = 53.20%
# CH3: PWM freq = 50.413223MHz, duty cycle = 54.23%
```