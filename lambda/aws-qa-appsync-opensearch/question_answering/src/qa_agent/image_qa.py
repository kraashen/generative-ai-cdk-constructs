#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
# and limitations under the License.
#

import json
import re
import boto3
import os
import base64
import base64

from llms import get_llm,get_bedrock_fm
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
from .sagemaker_endpoint import MultiModal
from aws_lambda_powertools import Logger, Tracer, Metrics
from .StreamingCallbackHandler import StreamingCallbackHandler

from .helper import  download_file, load_vector_db_opensearch,send_job_status, JobStatus,get_presigned_url,encode_image_to_base64


logger = Logger(service="QUESTION_ANSWERING")
tracer = Tracer(service="QUESTION_ANSWERING")
metrics = Metrics(namespace="question_answering", service="QUESTION_ANSWERING")




bucket_name = os.environ['INPUT_BUCKET']


def run_qa_agent_on_image_no_memory(input_params):
    logger.info("starting qa agent without memory on uploaded image")

    status_variables = {
        'jobstatus': JobStatus.WORKING.status,
        'answer': JobStatus.WORKING.get_message(),
        'jobid': input_params['jobid'],
        'filename': input_params['filename'],
        'question': input_params['question'],
        'sources': ['']
    }

    qa_model=input_params['qa_model']
    
    if 'modelId' in qa_model:
         qa_modelId= qa_model['modelId']
    else:
         logger.error(' No model id defined, cannot answer question , returning...')
         status_variables['jobstatus'] = JobStatus.ERROR_LOAD_LLM.status
         status_variables['answer'] = JobStatus.ERROR_LOAD_LLM.status
         send_job_status(status_variables)
         return status_variables


    filename = input_params['filename']
    status_variables['sources'] = [filename]
    
    if filename is None:
        status_variables['jobstatus'] = JobStatus.ERROR_LOAD_DOC.status
        error = JobStatus.ERROR_LOAD_DOC.get_message()
        status_variables['answer'] = error.decode("utf-8")
        send_job_status(status_variables)
        return status_variables
    
    logger.info(f"Generating response to question for file {filename} ")
    return process_visual_qa(input_params,status_variables,filename)
   
       

_doc_index = None
_current_doc_index = None

def run_qa_agent_rag_on_image_no_memory(input_params):
    logger.info("starting qa agent with rag approach without memory :: {input_params}")


    status_variables = {
        'jobstatus': JobStatus.WORKING.status,
        'answer': JobStatus.WORKING.get_message(),
        'jobid': input_params['jobid'],
        'filename': input_params['filename'],
        'question': input_params['question'],
        'sources': ['']
    }
    send_job_status(status_variables)

    filename = input_params['filename']
    logger.info(f"Generating response to question for file {filename}")

    qa_model=input_params['qa_model']
    embeddings_model = input_params['embeddings_model']
    
    if 'modelId' in qa_model and 'modelId' in embeddings_model:
         qa_model_id= qa_model['modelId']
         embedding_model_id = input_params['embeddings_model']['modelId']
    else:
         logger.error(' RAG based QA need both qa_model_id and embeddings_model_id, either one or both are missing, cannot answer question using RAG, returning...')
         status_variables['jobstatus'] = JobStatus.ERROR_LOAD_LLM.status
         status_variables['answer'] = JobStatus.ERROR_LOAD_LLM.status
         send_job_status(status_variables)
         return 
    
    
   
    global _doc_index
    global _current_doc_index

     # 1. OS similarity search, get image
    status_variables = get_image_from_semantic_search_in_os(input_params,status_variables)
    source_filename=status_variables['filename']
    # 2 : answer on searched image 
    status_variables = process_visual_qa(input_params,status_variables,source_filename)
   
    return status_variables

def get_image_from_semantic_search_in_os(input_params,status_variables):
    
    embeddings_model=input_params['embeddings_model']
    embedding_model_id = embeddings_model['modelId']
    modality=embeddings_model.get("modality", "Text")

    if _doc_index is None:
        logger.info("loading opensearch retriever")
        doc_index = load_vector_db_opensearch(boto3.Session().region_name,
                                              os.environ.get('OPENSEARCH_API_NAME'),
                                              os.environ.get('OPENSEARCH_DOMAIN_ENDPOINT'),
                                              os.environ.get('OPENSEARCH_INDEX'),
                                              os.environ.get('OPENSEARCH_SECRET_ID'),
                                              embedding_model_id,
                                              modality)

    else:
        logger.info("_retriever already exists")

    _current_doc_index = _doc_index

    max_docs = input_params['retrieval']['max_docs']
    output_file_name = input_params['filename']
    base64_bytes = input_params['question'].encode("utf-8")
    sample_string_bytes = base64.b64decode(base64_bytes)
    decoded_question = sample_string_bytes.decode("utf-8")

    source_documents = doc_index.similarity_search(decoded_question, k=max_docs)
    logger.info(f'source is :: {source_documents}')
    
    # if filename is given then filter the source_document for specfic file
    logger.info(f'output_file_name is :: {output_file_name}')
    if output_file_name:
        source_documents = [doc for doc in source_documents if doc.metadata['source'] == output_file_name]
    status_variables['sources'] = list(set(doc.metadata['source'] for doc in source_documents))
    
    # get new presigned url for RAG from source metadata
    if source_documents is not None:
        filename = source_documents[0].metadata['filename']
        status_variables['filename']=filename     
    
    
    return status_variables

def process_visual_qa(input_params,status_variables,filename):
    
    qa_model= input_params['qa_model']
    qa_modelId=qa_model['modelId']
    
    # default model provider is bedrock and defalut modality is tEXT
    modality=qa_model.get("modality", "Text")
    model_provider=qa_model.get("provider","Bedrock")
    logger.info(f"model provider is {model_provider} and modality is {modality}")  
   
    base64_bytes = input_params['question'].encode("utf-8")
    sample_string_bytes = base64.b64decode(base64_bytes)
    decoded_question = sample_string_bytes.decode("utf-8")
    
    if model_provider=='Sagemaker Endpoint':
        _qa_llm = MultiModal.sagemakerendpoint_llm(qa_modelId)   
        if(_qa_llm is not None):
             status_variables['answer']=generate_vision_answer_sagemaker(_qa_llm,input_params,decoded_question,filename,status_variables)
             status_variables['jobstatus'] = JobStatus.DONE.status             
        else:
            logger.error('Invalid Model , cannot load  LLM , returning..')
            status_variables['jobstatus'] = JobStatus.ERROR_LOAD_LLM.status
            error = JobStatus.ERROR_LOAD_LLM.get_message()
            status_variables['answer'] = error.decode("utf-8")
        
    elif model_provider=='Bedrock':
        _qa_llm=get_bedrock_fm(qa_modelId,modality)
        if(_qa_llm is not None):
            local_file_path= download_file(bucket_name,filename)
            base64_images=encode_image_to_base64(local_file_path,filename) 
            status_variables['answer']= generate_vision_answer_bedrock(_qa_llm,base64_images, qa_modelId,decoded_question)
            if(status_variables['answer'] is None):
                status_variables['answer'] = JobStatus.ERROR_PREDICTION.status
                error = JobStatus.ERROR_PREDICTION.get_message()
                status_variables['answer'] = error.decode("utf-8")
                status_variables['jobstatus'] = JobStatus.ERROR_PREDICTION.status
            else:    
                status_variables['jobstatus'] = JobStatus.DONE.status
                streaming = input_params.get("streaming", False)

        else:
            logger.error('Invalid Model , cannot load  LLM , returning..')
            status_variables['jobstatus'] = JobStatus.ERROR_LOAD_LLM.status
            error = JobStatus.ERROR_LOAD_LLM.get_message()
            status_variables['answer'] = error.decode("utf-8")
    else:
        logger.error('Invalid Model provider, cannot load  LLM , returning..')
        status_variables['jobstatus'] = JobStatus.ERROR_LOAD_LLM.status
        error = JobStatus.ERROR_LOAD_LLM.get_message()
        status_variables['answer'] = error.decode("utf-8")
    
    send_job_status(status_variables)
    return status_variables

def generate_vision_answer_sagemaker(_qa_llm,input_params,decoded_question,status_variables,filename):

    image_url = get_presigned_url(bucket_name,filename)
    if image_url is None:
        logger.error(f'cannot find image url, returning..') 
        status_variables['jobstatus'] = JobStatus.ERROR_LOAD_DOC.status
        error = JobStatus.ERROR_LOAD_DOC.get_message()
        status_variables['answer'] = error.decode("utf-8")
        send_job_status(status_variables)
        return status_variables

    template = """\n\nUser: {question}![]({image})<end_of_utterance>
         \n\nAssistant:"""
    verbose = input_params.get('verbose',False)
    prompt = PromptTemplate(template=template, input_variables=["image", "question"])
    chain = LLMChain(llm=_qa_llm, prompt=prompt, verbose=verbose)
    
    try:    
        logger.info(f'decoded_question is: {decoded_question}')
        tmp = chain.predict(image=image_url, question=decoded_question)
        answer=tmp.split("Assistant:",1)[1]
        
        logger.info(f'answer is: {answer}')
        llm_answer_bytes = answer.encode("utf-8")
        base64_bytes = base64.b64encode(llm_answer_bytes)
        llm_answer_base64_string = base64_bytes.decode("utf-8")

        status_variables['jobstatus'] = JobStatus.DONE.status
        status_variables['answer'] = llm_answer_base64_string
        
    except Exception as err:
        logger.exception(err)
        status_variables['jobstatus'] = JobStatus.ERROR_PREDICTION.status
        error = JobStatus.ERROR_PREDICTION.get_message()
        status_variables['answer'] = error.decode("utf-8")
    
    return status_variables

def generate_vision_answer_bedrock(bedrock_client,base64_images,model_id,decoded_question):
    system_prompt=""
    # use system prompt for fine tuning the performamce
    # system_prompt= """
    # You have perfect vision and pay great attention to detail which
    # makes you an expert at answering architecture diagram question. 
    # Answer question in <question></question> tags. Before answer, 
    # think step by step in <thinking> tags and analyze every part of the diagram.
    # """
    #Create a prompt with the question
    prompt =f"<question>{decoded_question}</question>. Answer must be a numbered list in a small paragraph inside <answer></answer> tag."

    claude_config = {
        'max_tokens': 1000, 
        'temperature': 0, 
        'anthropic_version': '',  
        'top_p': 1, 
        'stop_sequences': ['Human:']
        }
    
    messages = {    
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64_images
                }
            },
            {
                "type": "text",
                "text": prompt
            
            }
        ]
    }

    body=json.dumps({'messages': [messages],**claude_config, "system": system_prompt})
    try:
        response = bedrock_client.invoke_model(
        body=body, modelId=model_id, accept="application/json",
                        contentType="application/json"
                    )  
    except Exception as err:
        logger.exception(f'Error occurred , Reason :{err}')
        return None
    
    response = json.loads(response['body'].read().decode('utf-8'))

    formated_response= response['content'][0]['text']
    answer = re.findall(r'<answer>(.*?)</answer>', formated_response, re.DOTALL)
    formatted_answer=answer[0]
    llm_answer_bytes = formatted_answer.encode("utf-8")
    print(f' formatted_answer {formatted_answer}')
    base64_bytes = base64.b64encode(llm_answer_bytes)
    print(f' base64_bytes')
    llm_answer_base64_string = base64_bytes.decode("utf-8")

    print(f' llm_answer_base64_string {llm_answer_base64_string}')

    return llm_answer_base64_string

