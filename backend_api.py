"""
Backend API Wrapper for EPLC Assistant (Enhanced Version)
This module wraps the backend logic for the Streamlit frontend
Supports dual-database retrieval (EPLC + HHS) with exact + semantic search
"""
import os, streamlit as st
st.write("ENV has OPENAI_API_KEY:", bool(os.getenv("OPENAI_API_KEY")))

import os
import sys
from typing import List, Tuple
from dotenv import load_dotenv
from chromadb import PersistentClient
from sentence_transformers import SentenceTransformer
from openai import OpenAI

# Runtime environment settings
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

# System prompts
GEN_SYSTEM = """You are an assistant that drafts paste-ready text for a chosen phase of the Enterprise Product Lifecycle (EPLC).
Each vector database corresponds to a specific phase (Requirement, Design, Implementation, or Development).
Use the phase, template, and section to stay in scope.
Be concise, specific, and professional (120–180 words)."""

SYSTEM_PROMPT = (
    "You are an EPLC/HHS domain assistant. Answer questions using the provided CONTEXT. "
    "If the CONTEXT contains relevant information, synthesize an answer even if it's not explicitly stated. "
    "If the CONTEXT has NO relevant information at all, reply: Not specified in the provided context."
)

FALLBACK_PROMPT = (
    "You are a general expert assistant. Give a correct and helpful answer using general knowledge. "
    "DO NOT reference or imply context. DO NOT hallucinate context."
)


class EPLCBackend:
    """Backend handler for EPLC document generation and Q&A with dual-database support"""
    
    def __init__(self):
        """Initialize the backend with OpenAI, ChromaDB connections, and embedding model"""
        try:
            from dotenv import load_dotenv
            load_dotenv()  # 本地开发时读取 .env
        except Exception:
            pass  # 云端没 dotenv / 没 .env 也不影响

        
        
        # API Key validation
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY missing in .env file")
        
        # Configuration
        self.chat_model = os.getenv("CHAT_MODEL", "gpt-4o-mini")
        self.top_k = int(os.getenv("TOP_K", "6"))
        self.sim_filter = float(os.getenv("SIM_FILTER", "0.45"))
        self.min_sim = float(os.getenv("MIN_SIM", "0.35"))
        self.target_min = int(os.getenv("TARGET_MIN_WORDS", "120"))
        self.target_max = int(os.getenv("TARGET_MAX_WORDS", "180"))
        self.sem_threshold = float(os.getenv("SEM_THRESHOLD", "0.75"))
        
        # OpenAI client (使用美国区域端点)
        self.oa = OpenAI(
            api_key=self.api_key,
            base_url="https://us.api.openai.com/v1"
        )
        
        print(f"[DEBUG] Using base_url: {self.oa.base_url}", file=sys.stderr)
        
        # Database paths
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_ROOT = os.getenv("CHROMA_ROOT", os.path.join(BASE_DIR, "vector_db"))
        
        self.PHASE_PATHS = {
            "requirement": os.path.join(DB_ROOT, "Requirement_db"),
            "design": os.path.join(DB_ROOT, "Design_db"),
            "implementation": os.path.join(DB_ROOT, "Implementation_db"),
            "development": os.path.join(DB_ROOT, "Development_db"),
        }
        
        # Dual database paths for Q&A
        self.DB_EPLC_PATH = os.path.join(DB_ROOT, "EPLCFramework_db")
        self.DB_HHS_PATH = os.path.join(DB_ROOT, "HHS_db")
        
        # Initialize sentence transformer for semantic search
        try:
            self.sbert = SentenceTransformer("BAAI/bge-large-en-v1.5", device="cpu")
            print("[INFO] Sentence transformer loaded successfully", file=sys.stderr)
        except Exception as e:
            print(f"[WARNING] Could not load sentence transformer: {e}", file=sys.stderr)
            self.sbert = None
        
        # Connect to dual databases (for Q&A mode)
        self._init_qa_databases()
    
    def _init_qa_databases(self):
        """Initialize connections to EPLC and HHS databases for Q&A"""
        try:
            if os.path.exists(self.DB_EPLC_PATH):
                self.eplc_db = PersistentClient(path=self.DB_EPLC_PATH)
                self.coll_eplc = self._get_single_collection(self.eplc_db, "EPLCFramework_db")
                print("[INFO] EPLC database connected", file=sys.stderr)
            else:
                self.coll_eplc = None
                print("[WARNING] EPLC database not found", file=sys.stderr)
            
            if os.path.exists(self.DB_HHS_PATH):
                self.hhs_db = PersistentClient(path=self.DB_HHS_PATH)
                self.coll_hhs = self._get_single_collection(self.hhs_db, "HHS_db")
                print("[INFO] HHS database connected", file=sys.stderr)
            else:
                self.coll_hhs = None
                print("[WARNING] HHS database not found", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] Failed to initialize Q&A databases: {e}", file=sys.stderr)
            self.coll_eplc = None
            self.coll_hhs = None
    
    def _get_single_collection(self, db, label):
        """Get the single collection from a database"""
        cols = db.list_collections()
        if not cols:
            raise RuntimeError(f"[error] No collections in {label}")
        if len(cols) > 1:
            print(f"[WARNING] Multiple collections in {label}, using first one", file=sys.stderr)
        return cols[0]
    
    # ==================== Original OpenAI Embedding Methods ====================
    
    def embed_1024(self, text: str):
        """Generate 1024-dimensional embeddings for text using OpenAI"""
        try:
            resp = self.oa.embeddings.create(
                model="text-embedding-3-large",
                dimensions=1024,
                input=text,
            )
            return resp.data[0].embedding
        except Exception as e:
            print(f"[ERROR] Embedding failed: {e}", file=sys.stderr)
            raise
    
    def query_database(self, collection, text: str):
        """Query the vector database with embedded text (OpenAI embeddings)"""
        try:
            emb = self.embed_1024(text)
            res = collection.query(
                query_embeddings=[emb],
                n_results=self.top_k,
                include=["documents", "distances"],
            )
            docs = res.get("documents", [[]])[0]
            dists = res.get("distances", [[]])[0]
            return docs, dists
        except Exception as e:
            print(f"[retriever] query failed: {e}", file=sys.stderr)
            return [], []
    
    @staticmethod
    def dist_to_sim(d):
        """Convert distance to similarity score"""
        try:
            return 1.0 - float(d)
        except:
            return 0.0
    
    def filter_by_threshold(self, docs, dists):
        """Filter documents by similarity threshold"""
        return [d for d, dist in zip(docs, dists) 
                if self.dist_to_sim(dist) >= self.sim_filter]
    
    def join_context(self, docs):
        """Join context documents into a single string"""
        return "\n\n---\n\n".join(docs) if docs else "(no strong matches)"
    
    # ==================== New Dual Retrieval Methods ====================
    
    def retrieve_exact(self, substring: str, k: int = None) -> Tuple[List[str], List[str], List[float]]:
        """
        Exact keyword-based retrieval from both EPLC and HHS databases
        
        Args:
            substring: Keyword to search for
            k: Number of results to return (default: self.top_k)
        
        Returns:
            Tuple of (ids, documents, distances)
        """
        if k is None:
            k = self.top_k
        
        ids, docs = [], []
        
        for coll in (self.coll_eplc, self.coll_hhs):
            if coll is None:
                continue
            try:
                r = coll.get(
                    where_document={"$contains": substring},
                    include=["documents"],
                    limit=k,
                )
                ids.extend(r.get("ids", []))
                docs.extend(r.get("documents", []))
            except Exception as e:
                print(f"[WARNING] Exact retrieval failed: {e}", file=sys.stderr)
                continue
        
        return ids[:k], docs[:k], [0.0] * min(len(docs), k)
    
    def retrieve_semantic(self, query: str, k: int = None) -> Tuple[List[str], List[str], List[float]]:
        """
        Semantic retrieval using sentence transformers from both databases
        
        Args:
            query: Query text
            k: Number of results to return (default: self.top_k)
        
        Returns:
            Tuple of (ids, documents, distances)
        """
        if k is None:
            k = self.top_k
        
        if self.sbert is None:
            print("[WARNING] Sentence transformer not available, skipping semantic search", file=sys.stderr)
            return [], [], []
        
        try:
            # Encode query
            qv = self.sbert.encode([f"query: {query}"], normalize_embeddings=True).tolist()
            
            combined = []
            
            # Query EPLC database
            if self.coll_eplc is not None:
                try:
                    r1 = self.coll_eplc.query(
                        query_embeddings=qv, 
                        n_results=k,
                        include=["documents", "distances"]
                    )
                    ids1 = r1.get("ids", [[]])[0]
                    docs1 = r1.get("documents", [[]])[0]
                    dist1 = r1.get("distances", [[]])[0]
                    
                    for a, b, c in zip(ids1, docs1, dist1):
                        combined.append((a, b, c))
                except Exception as e:
                    print(f"[WARNING] EPLC semantic search failed: {e}", file=sys.stderr)
            
            # Query HHS database
            if self.coll_hhs is not None:
                try:
                    r2 = self.coll_hhs.query(
                        query_embeddings=qv, 
                        n_results=k,
                        include=["documents", "distances"]
                    )
                    ids2 = r2.get("ids", [[]])[0]
                    docs2 = r2.get("documents", [[]])[0]
                    dist2 = r2.get("distances", [[]])[0]
                    
                    for a, b, c in zip(ids2, docs2, dist2):
                        combined.append((a, b, c))
                except Exception as e:
                    print(f"[WARNING] HHS semantic search failed: {e}", file=sys.stderr)
            
            # Sort by distance and take top k
            combined.sort(key=lambda x: x[2])
            combined = combined[:k]
            
            return (
                [x[0] for x in combined],
                [x[1] for x in combined],
                [x[2] for x in combined]
            )
        
        except Exception as e:
            print(f"[ERROR] Semantic retrieval failed: {e}", file=sys.stderr)
            return [], [], []
    
    # ==================== Chat Generation Methods ====================
    
    def chat_generate(self, system, user):
        """Generate response using OpenAI chat completion"""
        try:
            resp = self.oa.chat.completions.create(
                model=self.chat_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"[ERROR] Chat generation failed: {e}", file=sys.stderr)
            raise
    
    def make_prompt(self, question: str, docs: List[str]) -> str:
        """Create a prompt with context and question"""
        ctx = "\n\n---\n\n".join(docs)
        return f"CONTEXT:\n{ctx}\n\nQUESTION:\n{question}\n"
    
    def ask_openai(self, prompt: str, allow_fallback: bool = False) -> str:
        """
        Ask OpenAI with optional fallback to general knowledge
        
        Args:
            prompt: The prompt to send
            allow_fallback: If True, use general knowledge when context is insufficient
        
        Returns:
            Generated answer text
        """
        sys_prompt = FALLBACK_PROMPT if allow_fallback else SYSTEM_PROMPT
        try:
            resp = self.oa.chat.completions.create(
                model=self.chat_model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            return f"[openai error] {e}"
    
    # ==================== Document Generation Method ====================
    
    def generate_document_section(self, phase: str, template: str, 
                                   section: str, details: str, 
                                   instructions: str = ""):
        """
        Generate document sections for EPLC phases
        
        Args:
            phase: EPLC phase (requirement/design/implementation/development)
            template: Document template name
            section: Section name to generate
            details: User-provided context/details
            instructions: Additional instructions (optional)
        
        Returns:
            dict: {'success': bool, 'draft': str, 'error': str}
        """
        try:
            phase = phase.lower()
            
            if phase not in self.PHASE_PATHS:
                return {
                    'success': False,
                    'error': f"Invalid phase. Must be one of: {', '.join(self.PHASE_PATHS.keys())}"
                }
            
            chroma_path = self.PHASE_PATHS[phase]
            if not os.path.exists(chroma_path):
                return {
                    'success': False,
                    'error': f"Database folder for phase '{phase}' not found at {chroma_path}"
                }
            
            # Connect to ChromaDB
            chroma_client = PersistentClient(path=chroma_path)
            collections = [c.name for c in chroma_client.list_collections()]
            
            if not collections:
                return {
                    'success': False,
                    'error': f"No collections found in {phase} database"
                }
            
            coll = chroma_client.get_collection(collections[0])
            
            # Set default instructions if not provided
            if not instructions:
                instructions = f"Concise, specific, {self.target_min}-{self.target_max} words."
            
            # Query the database
            query_text = f"{phase.title()} Phase | Template: {template} | Section: {section}\n{details}"
            docs, dists = self.query_database(coll, query_text)
            kept = self.filter_by_threshold(docs, dists)
            context = self.join_context(kept)
            
            # Generate the draft
            user_prompt = f"""
CONTEXT:
{context}

QUESTION:
Draft the {section} section for the {template} in the {phase.title()} Phase.

User details:
{details}

Instructions:
{instructions}
"""
            
            draft = self.chat_generate(GEN_SYSTEM, user_prompt)
            
            # Add assumptions if similarity is too low
            best_sim = max([self.dist_to_sim(d) for d in dists], default=0.0)
            if best_sim < (self.min_sim * 0.75):
                draft += (
                    "\n\nAssumptions & Next Steps:\n"
                    "- Confirm data categories and user groups.\n"
                    "- Validate environmental dependencies.\n"
                    "- List technical or security risks.\n"
                    "- Identify owner responsibilities.\n"
                )
            
            return {
                'success': True,
                'draft': draft,
                'error': None
            }
            
        except Exception as e:
            print(f"[ERROR] generate_document_section failed: {e}", file=sys.stderr)
            return {
                'success': False,
                'error': str(e),
                'draft': None
            }
    
    # ==================== Enhanced Q&A Method with Dual Retrieval ====================
    
    def answer_question(self, question: str, phase: str = None, use_dual_retrieval: bool = True):
        """
        Answer a general EPLC/HHS question with dual retrieval support
        
        Args:
            question: User's question
            phase: EPLC phase to search (optional, for single-phase queries)
            use_dual_retrieval: If True, use both exact and semantic search across EPLC+HHS
        
        Returns:
            dict: {'success': bool, 'answer': str, 'error': str, 'citations': list}
        """
        try:
            # Mode 1: Dual retrieval (EPLC + HHS with exact + semantic)
            if use_dual_retrieval and (self.coll_eplc is not None or self.coll_hhs is not None):
                return self._answer_with_dual_retrieval(question)
            
            # Mode 2: Single phase retrieval (original method)
            elif phase is not None:
                return self._answer_single_phase(question, phase)
            
            # Mode 3: Fallback to implementation phase
            else:
                return self._answer_single_phase(question, "implementation")
        
        except Exception as e:
            print(f"[ERROR] answer_question failed: {e}", file=sys.stderr)
            return {
                'success': False,
                'error': str(e),
                'answer': None,
                'citations': []
            }
    
    def _answer_with_dual_retrieval(self, question: str):
        """Answer using dual retrieval strategy (exact + semantic across EPLC+HHS)"""
        try:
            print("[DEBUG] Using dual retrieval mode", file=sys.stderr)
            
            # Step 1: Exact retrieval
            ids_exact, docs_exact, _ = self.retrieve_exact(question, self.top_k)
            
            # Step 2: Semantic retrieval
            ids_sem, docs_sem, dists_sem = self.retrieve_semantic(question, self.top_k)
            
            # Step 3: Filter semantic results by threshold
            sem_valid = [
                (i, d) for i, d, dist in zip(ids_sem, docs_sem, dists_sem)
                if dist < self.sem_threshold
            ]
            ids_sem = [x[0] for x in sem_valid]
            docs_sem = [x[1] for x in sem_valid]
            
            # Step 4: Combine results
            combined_ids = ids_exact + ids_sem
            combined_docs = docs_exact + docs_sem
            
            # with open("./tmp.txt", 'w') as f:
            #     for doc in combined_docs:
            #         f.write(doc)
            #         f.write("===================")
            
            # Step 5: Check if we have any context
            if not combined_docs:
                print("[DEBUG] No valid domain context → strict refusal", file=sys.stderr)
                return {
                    'success': True,
                    'answer': "Not specified in the provided context.",
                    'error': None,
                    'citations': []
                }
            
            # Step 6: Generate answer with strict context adherence
            prompt = self.make_prompt(question, combined_docs)
            answer = self.ask_openai(prompt, allow_fallback=False)
            
            # Step 7: Check if context was insufficient
            if answer.strip().lower() == "not specified in the provided context.":
                print("[DEBUG] Context exists but insufficient → fallback to general knowledge", file=sys.stderr)
                answer = self.ask_openai(question, allow_fallback=True)
            
            return {
                'success': True,
                'answer': answer,
                'error': None,
                'citations': combined_ids
            }
        
        except Exception as e:
            print(f"[ERROR] Dual retrieval failed: {e}", file=sys.stderr)
            return {
                'success': False,
                'error': str(e),
                'answer': None,
                'citations': []
            }
    
    def _answer_single_phase(self, question: str, phase: str):
        """Answer using single phase database (original method)"""
        try:
            phase = phase.lower()
            
            if phase not in self.PHASE_PATHS:
                phase = "implementation"  # Default fallback
            
            chroma_path = self.PHASE_PATHS[phase]
            if not os.path.exists(chroma_path):
                return {
                    'success': False,
                    'error': f"Database folder for phase '{phase}' not found",
                    'answer': None,
                    'citations': []
                }
            
            # Connect to ChromaDB
            chroma_client = PersistentClient(path=chroma_path)
            collections = [c.name for c in chroma_client.list_collections()]
            
            if not collections:
                return {
                    'success': False,
                    'error': "No collections found in database",
                    'answer': None,
                    'citations': []
                }
            
            coll = chroma_client.get_collection(collections[0])
            
            # Query the database
            docs, dists = self.query_database(coll, question)
            kept = self.filter_by_threshold(docs, dists)
            context = self.join_context(kept)
            
            # Generate answer
            system_prompt = """You are an EPLC (Enterprise Product Lifecycle) assistant. 
Answer questions based on the provided context from EPLC documentation and policies.
Be concise, accurate, and professional."""
            
            user_prompt = f"""
CONTEXT:
{context}

QUESTION:
{question}

Please provide a clear and helpful answer based on the context above.
"""
            
            answer = self.chat_generate(system_prompt, user_prompt)
            
            return {
                'success': True,
                'answer': answer,
                'error': None,
                'citations': []  # Single phase mode doesn't track citations
            }
            
        except Exception as e:
            print(f"[ERROR] Single phase retrieval failed: {e}", file=sys.stderr)
            return {
                'success': False,
                'error': str(e),
                'answer': None,
                'citations': []
            }


# ==================== CLI Interface (Optional) ====================

def main():
    """Command-line interface for testing"""
    try:
        backend = EPLCBackend()
        print(f"[ready] GPT={backend.chat_model} | top_k={backend.top_k}")
        print("Ask EPLC/HHS questions. Type 'exit' to quit.")
        print("Mode: Dual retrieval (EPLC + HHS with exact + semantic search)")
        
        while True:
            q = input("\nQ> ").strip()
            if q.lower() in ("exit", "quit"):
                break
            
            print("Processing...")
            result = backend.answer_question(q, use_dual_retrieval=True)
            
            if result['success']:
                print("A>", result['answer'])
                if result.get('citations'):
                    print("   citations:", result['citations'])
            else:
                print("ERROR:", result['error'])
    
    except Exception as e:
        print(f"[FATAL] Initialization failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":

    main()

