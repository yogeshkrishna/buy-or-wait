# Buy or Wait: Source

The [project README](../README.md) contains setup, data requirements, commands, results, and limitations. [ARCHITECTURE.md](ARCHITECTURE.md) describes the shared decision pipeline.

Run from the repository root with Python 3.12+:

```bash
python -m pip install -r code/requirements-ocr.txt
python code/main.py --dataset-dir /absolute/path/to/dataset --generate-output --ocr rapidocr --output-csv predictions.csv
python code/main.py --dataset-dir /absolute/path/to/dataset --validate-output predictions.csv --ocr rapidocr
```

The organizer dataset is supplied separately. The final prediction path uses local OCR and no remote model. Evaluation entry points and the original run's usage report are under [evaluation/](evaluation/).
