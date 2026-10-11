import json
import uuid
import spacy
from datasketch import MinHash, MinHashLSH
from pathlib import Path
from typing import Tuple

class CorpusManager:
    def __init__(self, filepath="data/corpus.jsonl", threshold=0.7):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if not self.filepath.exists(): self.filepath.touch()
        self.nlp = spacy.load("fr_core_news_sm", disable=["ner", "parser"]) 
        self.nlp.add_pipe("sentencizer")
        self.lsh = MinHashLSH(threshold=threshold, num_perm=128)
        self.documents = {} 
        self._build_index()

    def _build_index(self):
        """Loads existing corpus into memory and rebuilds the LSH index."""
        with open(self.filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip(): continue
                doc = json.loads(line)
                self.documents[doc["unique_id"]] = doc
                
                m = self._compute_minhash(doc["text"])
                self.lsh.insert(doc["unique_id"], m)

    def _compute_minhash(self, text: str, num_sentences: int = 3) -> MinHash:
        """Extracts the first N sentences, lemmatizes, and generates a MinHash signature."""
        doc = self.nlp(text)
        sentences = list(doc.sents)[:num_sentences]
        
        m = MinHash(num_perm=128)
        for sent in sentences:
            for token in sent:
                if not token.is_stop and not token.is_punct and not token.is_space:
                    m.update(token.lemma_.lower().encode('utf8'))
        return m

    def add_document(self, title: str, text: str) -> Tuple[bool, str]:
        """Validates uniqueness and appends to the JSONL corpus."""
        m = self._compute_minhash(text)
        duplicates = self.lsh.query(m)
        
        if duplicates:
            return False, f"Duplicate found mapping to ID: {duplicates[0]}"
            
        doc_id = str(uuid.uuid4())
        
        doc_data = {
            "unique_id": doc_id,
            "title": title,
            "text": text,
            "read_status": False
        }
        
        self.lsh.insert(doc_id, m)
        self.documents[doc_id] = doc_data
        
        with open(self.filepath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(doc_data) + '\n')
            
        return True, doc_id

    def get_articles_metadata(self):
        """Returns a lightweight list of articles for the sidebar."""
        return [
            {"id": doc_id, "title": doc["title"], "read_status": doc.get("read_status", False)} 
            for doc_id, doc in self.documents.items()
        ]

    def get_article(self, doc_id: str):
        """Retrieves the full document by ID."""
        return self.documents.get(doc_id)

    def mark_as_read(self, doc_id: str):
        """Updates the read status in memory and rewrites the JSONL file."""
        if doc_id in self.documents:
            self.documents[doc_id]["read_status"] = True
            self._rewrite_corpus()
            return True
        return False

    def _rewrite_corpus(self):
        """Utility to flush in-memory state to disk."""
        with open(self.filepath, 'w', encoding='utf-8') as f:
            for doc in self.documents.values():
                f.write(json.dumps(doc) + '\n')
