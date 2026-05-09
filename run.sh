uv run pdf2md.py --books-dir books --output-dir outputs --dpi 100 \
                   --ollama-url http://192.168.2.36:11434 --model glm-ocr:latest \
                   --max-width 1200

uv run python -c "from marker.models import load_all_models; load_all_models()"