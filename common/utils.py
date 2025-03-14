import re
from io import BytesIO
from typing import Any, Dict, List, Optional, Awaitable, Callable, Tuple, Type, Union
import requests
import os
from collections import OrderedDict

import docx2txt
import tiktoken

from langchain.embeddings import OpenAIEmbeddings
from langchain.docstore.document import Document
from langchain.llms import AzureOpenAI
from langchain.chat_models import AzureChatOpenAI
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema import BaseOutputParser, OutputParserException
from langchain.vectorstores import VectorStore
from langchain.vectorstores.faiss import FAISS
from langchain.chains import LLMChain
from langchain.memory import ConversationBufferMemory
from langchain.agents import create_csv_agent
from langchain.chains.question_answering import load_qa_chain
from langchain.chains.qa_with_sources import load_qa_with_sources_chain
from langchain.chains import ConversationalRetrievalChain
from langchain.chains.conversational_retrieval.prompts import CONDENSE_QUESTION_PROMPT
from langchain.tools import BaseTool
from langchain.prompts import PromptTemplate
from openai.error import AuthenticationError
from langchain.docstore.document import Document
from pypdf import PdfReader
from sqlalchemy.engine.url import URL
from langchain.sql_database import SQLDatabase
from langchain import SQLDatabaseChain
from langchain.agents import AgentExecutor, initialize_agent, AgentType
from langchain.tools import BaseTool
from langchain.utilities import BingSearchAPIWrapper
from langchain.agents import create_sql_agent
from langchain.agents.agent_toolkits import SQLDatabaseToolkit
from langchain.callbacks.base import BaseCallbackManager

try:
    from .prompts import (COMBINE_QUESTION_PROMPT, COMBINE_PROMPT, COMBINE_CHAT_PROMPT,
                          CSV_PROMPT_PREFIX, CSV_PROMPT_SUFFIX, MSSQL_PROMPT, MSSQL_AGENT_PREFIX, 
                          MSSQL_AGENT_FORMAT_INSTRUCTIONS, CHATGPT_PROMPT, BING_PROMPT_PREFIX)
except Exception as e:
    print(e)
    from prompts import (COMBINE_QUESTION_PROMPT, COMBINE_PROMPT, COMBINE_CHAT_PROMPT,
                          CSV_PROMPT_PREFIX, CSV_PROMPT_SUFFIX, MSSQL_PROMPT, MSSQL_AGENT_PREFIX, 
                          MSSQL_AGENT_FORMAT_INSTRUCTIONS, CHATGPT_PROMPT, BING_PROMPT_PREFIX)



# @st.cache_data
def parse_docx(file: BytesIO) -> str:
    text = docx2txt.process(file)
    # Remove multiple newlines
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text


# @st.cache_data
def parse_pdf(file: BytesIO) -> List[str]:
    pdf = PdfReader(file)
    output = []
    for page in pdf.pages:
        text = page.extract_text()
        # Merge hyphenated words
        text = re.sub(r"(\w+)-\n(\w+)", r"\1\2", text)
        # Fix newlines in the middle of sentences
        text = re.sub(r"(?<!\n\s)\n(?!\s\n)", " ", text.strip())
        # Remove multiple newlines
        text = re.sub(r"\n\s*\n", "\n\n", text)

        output.append(text)

    return output


# @st.cache_data
def parse_txt(file: BytesIO) -> str:
    text = file.read().decode("utf-8")
    # Remove multiple newlines
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text


# @st.cache_data
def text_to_docs(text: List[str]) -> List[Document]:
    """Converts a string or list of strings to a list of Documents
    with metadata."""
    if isinstance(text, str):
        # Take a single string as one page
        text = [text]
    page_docs = [Document(page_content=page) for page in text]

    # Add page numbers as metadata
    for i, doc in enumerate(page_docs):
        doc.metadata["page"] = i + 1

    # Split pages into chunks
    doc_chunks = []

    for doc in page_docs:
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            separators=["\n\n", "\n", ".", "!", "?", ",", " ", ""],
            chunk_overlap=0,
        )
        chunks = text_splitter.split_text(doc.page_content)
        for i, chunk in enumerate(chunks):
            doc = Document(
                page_content=chunk, metadata={"page": doc.metadata["page"], "chunk": i}
            )
            # Add sources a metadata
            doc.metadata["source"] = f"{doc.metadata['page']}-{doc.metadata['chunk']}"
            doc_chunks.append(doc)
    return doc_chunks


# @st.cache_data(show_spinner=False)
def embed_docs(docs: List[Document], chunks_limit: int=100, verbose: bool = False) -> VectorStore:
    """Embeds a list of Documents and returns a FAISS index"""
 
    # Select the Embedder model'
    if verbose: print("Number of chunks:",len(docs))
    embedder = OpenAIEmbeddings(deployment="text-embedding-ada-002", chunk_size=1) 
    
    if len(docs) > chunks_limit:
        docs = docs[:chunks_limit]
        if verbose: print("Truncated Number of chunks:",len(docs))

    index = FAISS.from_documents(docs, embedder)

    return index


def search_docs(index: VectorStore, query: str, k: int=4) -> List[Document]:
    """Searches a FAISS index for similar chunks to the query
    and returns a list of Documents."""

    # Search for similar chunks
    docs = index.similarity_search(query, k)
    return docs


def get_sources(answer: Dict[str, Any], docs: List[Document]) -> List[Document]:
    """Gets the source documents for an answer."""

    # Get sources for the answer
    source_keys = [s for s in answer["output_text"].split("SOURCES: ")[-1].split(", ")]

    source_docs = []
    for doc in docs:
        if doc.metadata["source"] in source_keys:
            source_docs.append(doc)

    return source_docs



def wrap_text_in_html(text: List[str]) -> str:
    """Wraps each text block separated by newlines in <p> tags"""
    if isinstance(text, list):
        # Add horizontal rules between pages
        text = "\n<hr/>\n".join(text)
    return "".join([f"<p>{line}</p>" for line in text.split("\n")])


# Returns the num of tokens used on a string
def num_tokens_from_string(string: str) -> int:
    encoding_name ='cl100k_base'
    """Returns the number of tokens in a text string."""
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens

# Returning the toekn limit based on model selection
def model_tokens_limit(model: str) -> int:
    """Returns the number of tokens limits in a text model."""
    if model == "gpt-35-turbo":
        token_limit = 3000
    elif model == "gpt-4":
        token_limit = 7000
    elif model == "gpt-4-32k":
        token_limit = 31000
    else:
        token_limit = 3000
    return token_limit

# Returns num of toknes used on a list of Documents objects
def num_tokens_from_docs(docs: List[Document]) -> int:
    num_tokens = 0
    for i in range(len(docs)):
        num_tokens += num_tokens_from_string(docs[i].page_content)
    return num_tokens


def get_search_results(query: str, indexes: list, k: int = 5) -> List[dict]:
    
    headers = {'Content-Type': 'application/json','api-key': os.environ["AZURE_SEARCH_KEY"]}

    agg_search_results = []
    
    for index in indexes:
        url = os.environ["AZURE_SEARCH_ENDPOINT"] + '/indexes/'+ index + '/docs'
        url += '?api-version={}'.format(os.environ["AZURE_SEARCH_API_VERSION"])
        url += '&search={}'.format(query)
        url += '&select=*'
        url += '&$top={}'.format(k)  # You can change this to anything you need/want
        url += '&queryLanguage=en-us'
        url += '&queryType=semantic'
        url += '&semanticConfiguration=my-semantic-config'
        url += '&$count=true'
        url += '&speller=lexicon'
        url += '&answers=extractive|count-3'
        url += '&captions=extractive|highlight-false'

        resp = requests.get(url, headers=headers)

        search_results = resp.json()
        agg_search_results.append(search_results)

    return agg_search_results
    

def order_search_results( agg_search_results: List[dict], reranker_threshold: int) -> OrderedDict:
    
    """Orders based on score the results from get_search_results function"""
    
    content = dict()
    ordered_content = OrderedDict()
    
    for search_results in agg_search_results:
        for result in search_results['value']:
            if result['@search.rerankerScore'] > reranker_threshold: # Show results that are at least 25% of the max possible score=4
                content[result['id']]={
                                        "title": result['title'],
                                        "chunks": result['pages'],
                                        "language": result['language'],
                                        "caption": result['@search.captions'][0]['text'],
                                        "score": result['@search.rerankerScore'],
                                        "name": result['metadata_storage_name'],
                                        "location": result['metadata_storage_path']                  
                                    }
    #After results have been filtered we will Sort and add them as an Ordered list
    for id in sorted(content, key= lambda x: content[x]["score"], reverse=True):
        ordered_content[id] = content[id]

    return ordered_content


def get_answer(llm: AzureChatOpenAI,
               docs: List[Document], 
               query: str, 
               language: str, 
               chain_type: str,
               memory: ConversationBufferMemory = None,
               callback_manager: BaseCallbackManager = None
              ) -> Dict[str, Any]:
    
    """Gets an answer to a question from a list of Documents."""

    # Get the answer
        
    if chain_type == "stuff":
        if memory == None:
            chain = load_qa_with_sources_chain(llm, chain_type=chain_type,
                                               prompt=COMBINE_PROMPT,
                                               callback_manager=callback_manager)
        else:
            chain = load_qa_with_sources_chain(llm, chain_type=chain_type, 
                                               prompt=COMBINE_CHAT_PROMPT,
                                               memory=memory,
                                               callback_manager=callback_manager)

    elif chain_type == "map_reduce":
        if memory == None:
            chain = load_qa_with_sources_chain(llm, chain_type=chain_type, 
                                               question_prompt=COMBINE_QUESTION_PROMPT,
                                               combine_prompt=COMBINE_PROMPT,
                                               callback_manager=callback_manager)
        else:
            chain = load_qa_with_sources_chain(llm, chain_type=chain_type, 
                                               question_prompt=COMBINE_QUESTION_PROMPT,
                                               combine_prompt=COMBINE_CHAT_PROMPT,
                                               memory=memory,
                                               callback_manager=callback_manager)
    else:
        print("Error: chain_type", chain_type, "not supported")
    
    answer = chain( {"input_documents": docs, "question": query, "language": language}, return_only_outputs=True)

    return answer


def run_agent(question:str, agent_chain: AgentExecutor) -> str:
    """Function to run the brain agent and deal with potential parsing errors"""
    
    try:
        return agent_chain.run(input=question)
    
    except OutputParserException as e:
        # If the agent has a parsing error, we use OpenAI model again to reformat the error and give a good answer
        chatgpt_chain = LLMChain(
                llm=agent_chain.agent.llm_chain.llm, 
                    prompt=PromptTemplate(input_variables=["error"],template='Remove any json formating from the below text, also remove any portion that says someting similar this "Could not parse LLM output: ". Reformat your response in beautiful Markdown. Just give me the reformated text, nothing else.\n Text: {error}'), 
                verbose=False
            )

        response = chatgpt_chain.run(str(e))
        return response
    

######## TOOL CLASSES #####################################
###########################################################
    
class DocSearchTool(BaseTool):
    """Tool for Azure GPT Smart Search Engine"""
    
    name = "@docsearch"
    description = "useful when the questions includes the term: @docsearch.\n"

    llm: AzureChatOpenAI
    indexes: List[str]
    k: int = 10
    response_language: str = "English"
    reranker_th: int = 1
    chunks_limit:int = 100
    similarity_k: int = 4

    
    def _run(self, query: str) -> str:

        try:
            agg_search_results = get_search_results(query, self.indexes, self.k)
            ordered_results = order_search_results(agg_search_results, reranker_threshold=self.reranker_th)
            docs = []
            for key,value in ordered_results.items():
                for page in value["chunks"]:
                    docs.append(Document(page_content=page, metadata={"source": value["location"]}))

            # Calculate number of tokens of our docs
            tokens_limit = model_tokens_limit(self.llm.deployment_name)

            if(len(docs)>0):
                num_tokens = num_tokens_from_docs(docs)
                if self.verbose:
                    print("Custom token limit for", self.llm.deployment_name, ":", tokens_limit)
                    print("Combined docs tokens count:",num_tokens)

            else:
                return "No Results Found in my knowledge base"

            if num_tokens > tokens_limit:
                index = embed_docs(docs, chunks_limit = self.chunks_limit, verbose=self.verbose)
                top_docs = search_docs(index, query, k = self.similarity_k)

                # Now we need to recalculate the tokens count of the top results from similarity vector search
                # in order to select the chain type: stuff or map_reduce

                num_tokens = num_tokens_from_docs(top_docs)
                if self.verbose:
                    print("Token count after similarity search:", num_tokens)
                chain_type = "map_reduce" if num_tokens > tokens_limit else "stuff"

            else:
                # if total tokens is less than our limit, we don't need to vectorize and do similarity search
                top_docs = docs
                chain_type = "stuff"

            if self.verbose:
                print("Chain Type selected:", chain_type)

            response = get_answer(llm=self.llm, query=query, docs=top_docs, chain_type=chain_type, language=self.response_language)
            
            answer = response['output_text']
            
            try:
                split_regex = re.compile(f"sources?:?\\W*", re.IGNORECASE)
                answer_text = split_regex.split(answer)[0]
                sources_list = split_regex.split(answer)[1].replace(" ","").split(",")

                sources_html = '<br><u>Sources</u>: '
                for index, value in enumerate(sources_list):
                    url = value + os.environ["DATASOURCE_SAS_TOKEN"]
                    sources_html +='<sup><a href="'+ url + '">[' + str(index+1) + ']</a></sup>'
                    
                answer = answer_text + sources_html

            except Exception as e:
                print(e)
                
            return answer

        
        except Exception as e:
            print(e)
    
    async def _arun(self, query: str) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("DocSearchTool does not support async")
    

class CSVTabularTool(BaseTool):
    """Tool CSV agent"""
    
    name = "@csvfile"
    description = "useful when the questions includes the term: @csvfile.\n"

    path: str
    llm: AzureChatOpenAI
    
    def _run(self, query: str) -> str:
        
        try:
            agent = create_csv_agent(self.llm, self.path, verbose=self.verbose, callback_manager=self.callbacks,)
            for i in range(5):
                try:
                    response = agent.run(CSV_PROMPT_PREFIX + query + CSV_PROMPT_SUFFIX) 
                    break
                except:
                    response = "Error too many failed retries"
                    continue

            return response
        except Exception as e:
            print(e)
            response = e
            return response
    
    async def _arun(self, query: str) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("CSVTabularTool does not support async")
        
        
class SQLDbTool(BaseTool):
    """Tool SQLDB Agent"""
    
    name = "@covidstats"
    description = "useful when the questions includes the term: @covidstats.\n"

    llm: AzureChatOpenAI
    
    def _run(self, query: str) -> str:
        db_config = {
            'drivername': 'mssql+pyodbc',
            'username': os.environ["SQL_SERVER_USERNAME"] +'@'+ os.environ["SQL_SERVER_ENDPOINT"],
            'password': os.environ["SQL_SERVER_PASSWORD"],
            'host': os.environ["SQL_SERVER_ENDPOINT"],
            'port': 1433,
            'database': os.environ["SQL_SERVER_DATABASE"],
            'query': {'driver': 'ODBC Driver 17 for SQL Server'}
        }

        db_url = URL.create(**db_config)
        db = SQLDatabase.from_uri(db_url)
        toolkit = SQLDatabaseToolkit(db=db, llm=self.llm)
        agent_executor = create_sql_agent(
            prefix=MSSQL_AGENT_PREFIX,
            format_instructions = MSSQL_AGENT_FORMAT_INSTRUCTIONS,
            llm=self.llm,
            toolkit=toolkit,
            callback_manager=self.callbacks,
            verbose=self.verbose
        )

        for i in range(2):
            try:
                response = agent_executor.run(query) 
                break
            except Exception as e:
                response = str(e)
                continue

        return response
        
    
    async def _arun(self, query: str) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("SQLDbTool does not support async")
        
        
        
class ChatGPTTool(BaseTool):
    """Tool for a ChatGPT clone"""
    
    name = "@chatgpt"
    description = "useful when the questions includes the term: @chatgpt.\n"

    llm: AzureChatOpenAI
    
    def _run(self, query: str) -> str:
        try:
            chatgpt_chain = LLMChain(
                llm=self.llm, 
                prompt=CHATGPT_PROMPT,
                callback_manager=self.callbacks,
                verbose=self.verbose
            )

            response = chatgpt_chain.run(query)

            return response
        except Exception as e:
            print(e)
            
    async def _arun(self, query: str) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("ChatGPTTool does not support async")
        

# class BingSearchTool(BaseTool):
#     """Tool for a Bing Search Wrapper"""
    
#     name = "@bing"
#     description = "useful when the questions includes the term: @bing.\n"

#     llm: AzureChatOpenAI
#     k: int = 5
    
#     def _run(self, query: str) -> str:
#         try:
#             bing = BingSearchAPIWrapper(k=self.k)
#             context = str(bing.results(query,num_results=self.k))
#             chatgpt_chain = LLMChain(
#                 llm=self.llm, 
#                 prompt=PromptTemplate(input_variables=["context","question"],template='Only using the following pieces of texts with its corresponding titles and links: \n "{context}".\n Answer this question: {question}.\n\n If the answer to the question is not in the above pieces of texts, say "The answer is not in the context provided". Do not make up an answer. If you can provide the answer based on the pieces of texts, please also provide the link along with the answer. Nothing else.'),
#                 callback_manager=self.callbacks,
#                 verbose=self.verbose
#             )

#             response = chatgpt_chain({"question": query, "context": context})['text']

#             return response
        
#         except Exception as e:
#             print(e)
            
#     async def _arun(self, query: str) -> str:
#         """Use the tool asynchronously."""
#         raise NotImplementedError("ChatGPTTool does not support async")
    
    
class BingSearchResults(BaseTool):
    """Tool for a Bing Search Wrapper"""

    name = "@bing"
    description = "useful when the questions includes the term: @bing.\n"

    k: int = 5

    def _run(self, query: str) -> str:
        bing = BingSearchAPIWrapper(k=self.k)
        return bing.results(query,num_results=self.k)
    
    async def _arun(self, query: str) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("BingSearchResults does not support async")
            

class BingSearchTool(BaseTool):
    """Tool for a Bing Search Wrapper"""
    
    name = "@bing"
    description = "useful when the questions includes the term: @bing.\n"
    
    llm: AzureChatOpenAI
    k: int = 5
    
    def _run(self, tool_input: Union[str, Dict],) -> str:
        try:
            tools = [BingSearchResults(k=self.k)]
            parsed_input = self._parse_input(tool_input)

            agent_executor = initialize_agent(tools=tools, 
                                              llm=self.llm, 
                                              agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION, 
                                              agent_kwargs={'prefix':BING_PROMPT_PREFIX},
                                              callback_manager=self.callbacks,
                                              verbose=self.verbose)
            
            for i in range(3):
                try:
                    response = agent_executor.run(parsed_input) 
                    break
                except Exception as e:
                    response = str(e)
                    continue

            return response
        
        except Exception as e:
            print(e)
    
    async def _arun(self, query: str) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("BingSearchTool does not support async")