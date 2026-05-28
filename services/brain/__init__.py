"""脑区管线初始化。"""
from .input_zone import process_input
from .prefrontal import evaluate_and_gate
from .hippocampus import encode_or_merge
from .storage_zone import commit_to_storage
from .output_zone import retrieve_and_format
