# coding=utf-8

import json
import tqdm
import torch
from typing import List, final
from transformers.tokenization_utils import PreTrainedTokenizer
import numpy as np
import random
from functools import partial
from torch.utils.data import DataLoader, Sampler, Dataset
from torch.nn.utils.rnn import pad_sequence
from math import ceil
from inputters.inputter_dqn import _norm, BucketSampler, BucketingDataLoader, DistributedBucketingDataLoader
from .PARAMS import GOLDEN_TRUTH


class Inputter(object):
    def __init__(self):
        # prepare
        self.convert_data_to_inputs = convert_data_to_inputs
        self.convert_inputs_to_features = convert_inputs_to_features
        
        # train
        self.train_sampler = BucketSampler
        self.train_dataset = FeatureDataset
        self.train_dataloader = BucketingDataLoader
        self.train_distributed_dataloader = DistributedBucketingDataLoader
        
        # valid
        self.valid_dataloader = DynamicBatchingLoader
        
        # infer
        self.prepare_infer_batch = prepare_infer_batch
        self.infer_dataloader = get_infer_batch


# basic utils
class InputFeatures(object):
    def __init__(
        self,
        input_ids, strat_hist, sentiment_hist,
        next_sentence, next_sentiment_hist, next_strat_hist,
        decoder_input_ids, labels, returns, ls_ids, reward
    ):
        self.input_ids = input_ids
        self.input_length = len(input_ids)
        self.last_sentence_ids = ls_ids
        self.last_sentence_length = len(ls_ids)
        self.strat_hist = strat_hist
        self.sentiment_hist = sentiment_hist
        self.next_sentence = next_sentence
        self.next_sentence_len = len(next_sentence)
        self.next_sentiment_hist = next_sentiment_hist
        self.next_strat_hist = next_strat_hist
        self.decoder_input_ids = decoder_input_ids
        self.decoder_input_length = len(decoder_input_ids)
        self.labels = labels
        self.reward = reward
        self.returns = returns
        self.input_len = self.input_length + self.decoder_input_length


def featurize(
    bos, eos, returns, strat_hist, sentiment_hist,#bos: beginning of sentence?
    next_sentence, next_sentiment_hist, next_strat_hist, reward,
    context, max_input_length, last_sentence,#context is from both seekers and supporters
    response, max_decoder_input_length, strat_id, #response is from seekers?
):
    context = [c + [eos] for c in context] #
    input_ids = sum(context, [])[:-1] #flatten
    input_ids = input_ids[-max_input_length:]
    
    # ls_ids = [ls + [eos] for ls in last_sentence] #multi usr rounds
    # ls_ids = sum(last_sentence, [])[:-1]
    ls_ids = last_sentence[-max_input_length:]
    #ls_ids = sum()
    # next_sentence = [nc + [eos] for nc in next_sentence]
    # next_sentence = sum(next_sentence, [])[:-1]
    next_sentence = next_sentence[-max_input_length:]

    labels = ([strat_id] + response + [eos])[:max_decoder_input_length + 1] #ground truth
    decoder_input_ids = [bos] + labels[:-1] # bos+strat_id+reponse No eos
    
    assert len(decoder_input_ids) == len(labels), decoder_input_ids[1:] == labels[:-1]

    return InputFeatures(
        input_ids, strat_hist, sentiment_hist,
        next_sentence, next_sentiment_hist, next_strat_hist,
        decoder_input_ids, labels, returns, ls_ids, reward,
    )

#def 

def calc_sentiment_change(sentiment_seq):
    '''
    sentiment_seq: a list of sentiment of usrs' response. Every two elements can be seen as a pre and post system response
    sentiment score. In other words, the difference between every two neighbor elements is the delta of sentiment scorse
    of usrs after receive a system response

    return: a list of sentiment scores change values. Becasue, as we mentioned before, each of the change values is determined
    by two neighbor elements of the sentiment sequences, we expect the length of the list of sentiment-score-changes is the
    length of the input sequences minus one.
    '''
    sentiment_changes = []
    counter = 0
    #assert 
    #if None in sentiment_seq:
        #print(sentiment_seq)
    for i in range(1, len(sentiment_seq)):
        #print(sentiment_seq[i])
        if sentiment_seq[i] is np.nan:
            print(sentiment_seq)
        assert type(np.array(sentiment_seq[i])) == np.ndarray, f'ccccccjnmcvkxcnmfvkjnmdxasf,lvajnmso {sentiment_seq[i]}'
        assert len(np.array(sentiment_seq[i])) == 3, f'worinima {np.array(sentiment_seq[i])} length {len(np.array(sentiment_seq[i]))}'
        tmp = np.sum(np.array(sentiment_seq[i]) - np.array(sentiment_seq[i-1]))
        if tmp == 0:
            #This will happen when there are multi-turn sys responses without usr interaction
            #In such case, we assume these sys responses cause the same delta change of sentiment
            #tmp = sentiment_seq[i]
            counter += 1
            continue
        if counter:
            for _ in range(counter+1):
                sentiment_changes.append(np.array(sentiment_seq[i]) - np.array(sentiment_seq[i-1]))
            counter = 0
        else:
            sentiment_changes.append(np.array(sentiment_seq[i]) - np.array(sentiment_seq[i-1]))
    for _ in range(counter):
        sentiment_changes.append(sentiment_changes[-1])
    counter = 0
    assert len(sentiment_seq) - 1 == len(sentiment_changes), 'The legnth of the sentiment-score-changes is not len(sentiment_seq) - 1'
    
    return sentiment_changes


def convert_data_to_inputs(data, toker: PreTrainedTokenizer, **kwargs):
    process = lambda x: toker.convert_tokens_to_ids(toker.tokenize(x))
    # data2 usr rating input, data1 sentiment scores input
    data1, data2 = data
    dialog2, dialog1 = data2['dialog'], data1['dialog']
    inputs = []
    context = []

        #compute return reversely
    stress_level_change = data2['init_intensity'] - data2['final_intensity']
    relevance = data2['relevance']
    empathy = data2['empathy']
    final_fusion = (stress_level_change/5+relevance/5+empathy/5)/3 # [0,1]
    gamma = 0.95
    #returns = [final_fusion*gamma**n for n in range(len(dialog))][::-1]
    returns = []
    usr_rating = []
    sentiment = []
    sentiment_buffer = []
    usr_flag = 0
    last_sentence = process(_norm('Empty session'))
    next_sentence = process(_norm('End of the session'))
    next_sentiment = None
    sentiment_hist = [np.array([0,0,0]) for _ in range(5)] # 5 recent usr sentiments
    strat_hist = [process('[Blank]')[0] for _ in range(5) ] #keep last 5 steps of strats
    # note, if there is no history strat, we use a extra token 7 to represent it.
    # So we had 7 strat + 1 non-strat, that's 8 strats in total
    sys_counter, usr_counter, counter = 0, 0, 0 # count the num of sys sentence in between two ratings
    for i in range(len(dialog1)):
        text = _norm(dialog1[i]['text'])
        text = process(text)

        if dialog1[i]['speaker'] == 'sys':
            strat_id = process('[' + dialog1[i]['strategy'] + ']')
            assert len(strat_id) == 1
            strat_id = strat_id[0]
            #add one return only in sys's turn
            returns.append(final_fusion*gamma**counter)
            # since the first sentiment value has no earlier value, we need to record it first
            sys_counter += 1
            if not usr_flag: #if no usr text before at begining of the dialogue
                sentiment.append(np.array([1/3, 1/3, 1/3]))
                # beg_text = _norm('Begining of the session')
                # beg_text = process(beg_text)
                # last_sentence = beg_text
            else:
                #for _ in range(sys_counter):
                if sentiment_buffer:
                    sentiment.append(np.nanmean(sentiment_buffer, axis=0))
                # else:
                #     sentiment_buffer

            if i+1 < len(dialog1) and dialog1[i+1]['speaker'] == 'usr':
                sentiment_hist.pop(0)
                next_sentence = process(_norm(dialog1[i+1]['text']))
                #print(f'*****{next_sentence}')
                next_sentiment = dialog1[i+1]['score']
                if sentiment_buffer:
                    sentiment_hist.append(np.nanmean(sentiment_buffer, axis=0))
                else:
                    sentiment_hist.append(sentiment_hist[-1])
                sentiment_buffer = []
            counter += 1 #for long turn return gamma
            
            #sys_counter2 += 1
        elif dialog2[i]['speaker'] == 'usr':
            usr_flag = 1
            if dialog2[i]['rating'] is not None:
                for _ in range(sys_counter):
                    usr_rating.append(int(dialog2[i]['rating'])/5)
                sys_counter = 0
            if dialog1[i]['score'] is not None:
                #if sys_counter2 == 0: 
                    #multi-turn usr input case, take the avg sentiment of the last few rounds
                sentiment_buffer.append(dialog1[i]['score'])
                    # since the first sentiment value has no earlier value, we need to record it first
                #     if i == 0:
                #          sentiment.append(np.mean(sentiment_buffer, axis=0))
                #          sentiment_buffer

                # else:
                #     # since we only reach here when the speaker is usr, then there must be one round 
                #     # that we need to add the old score in buffer and clear it while we also add the 
                #     # current new score in buffer
                #     for _ in range(sys_counter2):
                #         sentiment.append(np.mean(sentiment_buffer, axis=0))
                #     sentiment_buffer = [dialog1[i]['score']]
                #     sys_counter2 = 0
            


            #usr_rating.append(dialog[i]['rating'])
            # usr_rating only scored every sys_counter sys term
        # elif dialog[i]['speaker'] == 'user' and dialog[i]['']:
        #     usr_rating.append(0)
        #     usr_rating.append(0)
        
        if i > 0 and dialog1[i]['speaker'] == 'sys':
            tmp_strat_hist = strat_hist.copy() # prepare for next state
            tmp_strat_hist.pop(0)
            tmp_strat_hist.append(strat_id)
            
            if i+1 >= len(dialog1): #prevent empty next sentence
                end_text = _norm('End of the session')
                end_text = process(end_text)
                next_sentence = end_text
                next_sentiment = sentiment_hist[-1]
                #print(f'----{next_sentence}')

            if next_sentiment is not None:
                tmp_sent_hist = sentiment_hist.copy()
                tmp_sent_hist.pop(0)
                tmp_sent_hist.append(np.array(next_sentiment))
            else:
                tmp_sent_hist = sentiment_hist.copy()



            if i+1 >= len(dialog1) or (i+2 >= len(dialog1) and dialog1[i+1]['speaker'] == 'usr'):
                reward = -stress_level_change # 0 - 5 larger is better
                sentiment_delta = sentiment_hist[-1] - sentiment_hist[-2]
                sentiment_delta = sentiment_delta[0] + sentiment_delta[1] - sentiment_delta[2]
                reward += sentiment_delta
            else:
                sentiment_delta = sentiment_hist[-1] - sentiment_hist[-2]
                sentiment_delta = sentiment_delta[0] + sentiment_delta[1] - sentiment_delta[2]
                reward = sentiment_delta

            res = {
                #'dialogid': i,
                'context': context.copy(),
                'response': text,
                'last_sentence': last_sentence,
                'next_sentence': next_sentence,
                'next_sentiment_hist': tmp_sent_hist[:],
                'next_strat_hist': tmp_strat_hist[:],
                'strat_id': strat_id,
                'reward': reward,
                'strat_hist': strat_hist.copy(),
                'sentiment_hist': sentiment_hist.copy(),
            }
            strat_hist.pop(0)
            strat_hist.append(strat_id)
            inputs.append(res)
            if i+1 < len(dialog1) and dialog1[i+1]['speaker'] == 'usr':
                last_sentence = None

        if dialog1[i]['speaker'] == 'sys':
            text = [strat_id] + text
        if dialog1[i]['speaker'] == 'usr':
            last_sentence = text

        context = context + [text] #No limitation for the length of the context?

    if sentiment_buffer:
        # in case the last round is from usr
        sentiment.append(np.nanmean(sentiment_buffer, axis=0))
    else:
        # If the last round is from sys, we assume it dosen't change any sentiment,
        # so we append the last sentiment score agian
        for _ in sys_counter:
            sentiment.append(sentiment[-1])
        
    #if True in np.isnan(np.array(sentiment)):
    #    print (f'asdfadf{sentiment}')
    if sys_counter > 0: #or sys_counter2 > 0:
        for _ in range(sys_counter):
            usr_rating.append(np.mean(usr_rating))#.append(int(dialog[i]['rating'])/5)
        # for _ in range(sys_counter2):
        #     sentiment.append(np.mean(sentiment_buffer, axis=0))
            #### when the last response is given by sys, then the last sentiment change is 0

        sys_counter = 0
    assert len(usr_rating) == len(returns), f'{usr_rating}, {returns} not match'
    
    sentiment_changes = calc_sentiment_change(sentiment)   
    # if len(usr_rating) != len(sentiment_changes):
    #     print('haha')
    assert len(usr_rating) == len(sentiment_changes), f'{len(usr_rating)}, {len(sentiment_changes)} not match, {dialog1}'
    #TODO change the following two variables as Hyper-parameters
    SENT_WEIGHT, RATE_WEIGHT = 0.6, 0.4
    sentiment_changes_post = list(map(lambda x: x[0]+x[1]-x[2], sentiment_changes))
    returns = list(np.array(returns[::-1]) + (RATE_WEIGHT*np.array(usr_rating) + SENT_WEIGHT*np.array(sentiment_changes_post)))
    #print(returns)
    #inputs.append(returns)
    return inputs, returns


def convert_inputs_to_features(inputs, returns, toker, **kwargs):
    if len(inputs) == 0:
        return []

    assert kwargs.get('max_input_length', None) is not None, 'you should give max_input_length'
    max_input_length = kwargs.get('max_input_length')
    assert kwargs.get('max_decoder_input_length', None) is not None, 'you should give max_decoder_input_length'
    max_decoder_input_length = kwargs.get('max_decoder_input_length')
    
    pad = toker.pad_token_id
    if pad is None:
        pad = toker.eos_token_id
        assert pad is not None, 'either pad_token_id or eos_token_id should be provided'
    bos = toker.bos_token_id
    if bos is None:
        bos = toker.cls_token_id
        assert bos is not None, 'either bos_token_id or cls_token_id should be provided'
    eos = toker.eos_token_id
    if eos is None:
        eos = toker.sep_token_id
        assert eos is not None, 'either eos_token_id or sep_token_id should be provided'
    
    features = []
    for i in range(len(inputs)):
        ipt = inputs[i]
        #dialogid = ipt['dialogid']
        feat = featurize(
            bos, eos, returns[i], ipt['strat_hist'], ipt['sentiment_hist'],
            ipt['next_sentence'], ipt['next_sentiment_hist'],
            ipt['next_strat_hist'], ipt['reward'],
            ipt['context'], max_input_length, ipt['last_sentence'],
            ipt['response'], max_decoder_input_length, ipt['strat_id'],
        )
        features.append(feat)
    return features


# for training
class FeatureDataset(Dataset):
    def __init__(self, features):
        self.features = features

    def __getitem__(self, i):
        return self.features[i]

    def __len__(self):
        return len(self.features)

    @staticmethod
    def collate(features: List[InputFeatures], toker: PreTrainedTokenizer, infer=False):
        pad = toker.pad_token_id
        if pad is None:
            pad = toker.eos_token_id
            assert pad is not None, 'either pad_token_id or eos_token_id should be provided'
        bos = toker.bos_token_id
        if bos is None:
            bos = toker.cls_token_id
            assert bos is not None, 'either bos_token_id or cls_token_id should be provided'
        eos = toker.eos_token_id
        if eos is None:
            eos = toker.sep_token_id
            assert eos is not None, 'either eos_token_id or sep_token_id should be provided'
        
        input_ids = pad_sequence([torch.tensor(f.input_ids, dtype=torch.long) for f in features],
                          batch_first=True, padding_value=pad)
        ls_ids =  pad_sequence([torch.tensor(f.last_sentence_ids, dtype=torch.long) for f in features],
                          batch_first=True, padding_value=pad)                         
        attention_mask = pad_sequence([torch.tensor([1.] * f.input_length, dtype=torch.float) for f in features],
                          batch_first=True, padding_value=0.)
        attention_mask_ls = pad_sequence([torch.tensor([1.] * f.last_sentence_length, dtype=torch.float) for f in features],
                          batch_first=True, padding_value=0.)
        next_ids = pad_sequence([torch.tensor(f.next_sentence, dtype=torch.long) for f in features],
                          batch_first=True, padding_value=pad) 
        attention_mask_next = pad_sequence([torch.tensor([1.] * f.next_sentence_len, dtype=torch.float) for f in features],
                    batch_first=True, padding_value=0.)

        input_length = torch.tensor([f.input_length for f in features], dtype=torch.long)
        input_length_ls = torch.tensor([f.last_sentence_length for f in features], dtype=torch.long)
        rewards = torch.tensor([f.returns for f in features], dtype=torch.float )
        sentiment_hist = torch.tensor([f.sentiment_hist for f in features], dtype=torch.float)
        tmp = [f.next_sentiment_hist for f in features]
        kkk = torch.tensor(tmp, dtype=torch.float)
        next_sentiment_hist = torch.tensor([f.next_sentiment_hist for f in features], dtype=torch.float)
        
        if not infer:
            decoder_input_ids = pad_sequence([torch.tensor(f.decoder_input_ids, dtype=torch.long) for f in features],
                              batch_first=True, padding_value=pad)
            labels = pad_sequence([torch.tensor(f.labels, dtype=torch.long) for f in features],
                              batch_first=True, padding_value=-100)
        else:
            decoder_input_ids = torch.tensor([[f.decoder_input_ids[0]] for f in features], dtype=torch.long)
            labels = None
        #print(len(toker))
        strat_id = torch.tensor([f.labels[0] for f in features], dtype=torch.long) - len(toker) + 9 # why -len(toker) + 8
        strat_hist = torch.tensor([f.strat_hist for f in features], dtype=torch.long)- len(toker) + 9
        next_strat_hist =  torch.tensor([f.next_strat_hist for f in features], dtype=torch.long)- len(toker) + 9
        reward_dqn = torch.tensor([f.reward for f in features], dtype=torch.float )

        res = {
            'input_ids': input_ids,
            'last_sentence': ls_ids,
            'attention_mask_ls': attention_mask_ls,
            'ls_lengths': input_length_ls,
            'attention_mask': attention_mask,
            'next_sentence': next_ids,
            'attention_mask_nx': attention_mask_next,
            'next_sentiment_hist': next_sentiment_hist,
            'next_strat_hist': next_strat_hist,
            'input_length': input_length,
            'strat_hist': strat_hist,
            'sentiment_hist': sentiment_hist,
            'decoder_input_ids': decoder_input_ids,
            'labels': labels,
            'reward': reward_dqn,
            'strat_id': strat_id,
        }
        
        return res


# for validation
class DynamicBatchingLoader(object):
    """ this loader takes raw text file, used for validate perplexity """
    def __init__(self, corpus_file, toker, batch_size, **kwargs):
        self.corpus = corpus_file
        self.toker = toker
        self.bs = batch_size
        self.num_examples = self.get_len(corpus_file)
        self.kwargs = kwargs

    def __iter__(self, epoch=1):
        if epoch > 0:
            for epoch in range(epoch):
                yield from self._iter_epoch()
        else:
            while True:
                yield from self._iter_epoch()

    def __len__(self):
        return ceil(self.num_examples / self.bs)

    def _iter_epoch(self):
        try:
            with open(self.corpus, 'r', encoding='utf-8') as f:
                reader = f.readlines()
            with open(self.corpus, 'r', encoding="utf-8") as f2:
                reader2 = f2.readlines()

            features = []
            for line in tqdm.tqdm(zip(reader, reader2), total=len(reader), desc=f"validating"):
                data = (json.loads(line[0]), json.loads(line[1]))
                inputs, returns = convert_data_to_inputs(data, self.toker, **self.kwargs)
                features.extend(convert_inputs_to_features(inputs, returns, self.toker, **self.kwargs))
                if len(features) >= self.bs:
                    batch = self._batch_feature(features)
                    yield batch
                    features = []
                    
            if len(features) > 0:
                batch = self._batch_feature(features)
                yield batch
                
        except StopIteration:
            pass
    
    def _batch_feature(self, features):
        return FeatureDataset.collate(features, self.toker)

    def get_len(self, corpus):
        with open(corpus, 'r', encoding="utf-8") as file:
            reader = [json.loads(line) for line in file]
        return sum(map(lambda x: len(list(filter(lambda y: y['speaker'] == 'sys', x['dialog'][1:]))), reader))


# for inference
def prepare_infer_batch(features, toker, interact=None):
    res = FeatureDataset.collate(features, toker, True)
    
    res['batch_size'] = res['input_ids'].size(0)

    other_res = res['other_res'] = {}
    other_res['acc_map'] = {
        'cls_strat_id': 'pred_strat_id',
    }

    if interact is None and GOLDEN_TRUTH:
        other_res['cls_strat_id'] = res.get('strat_id')
    else:
        other_res['cls_strat_id'] = res.pop('strat_id')

    return res


def get_infer_batch(infer_input_file, toker, **kwargs):
    assert 'infer_batch_size' in kwargs, 'you should give infer_batch_size'
    infer_batch_size = kwargs.get('infer_batch_size')

    with open(infer_input_file, 'r', encoding="utf-8") as f:
        reader = f.readlines()
    
    features = []
    sample_ids = []
    posts = []
    references = []
    for sample_id, line in tqdm.tqdm(enumerate(reader), total=len(reader), desc=f"inferring"):
        data = json.loads(line)
        inputs = convert_data_to_inputs(data, toker, **kwargs)
        tmp_features = convert_inputs_to_features(inputs, toker, **kwargs)
        for i in range(len(inputs)):
            features.append(tmp_features[i])
            ipt = inputs[i]
            posts.append(toker.decode(ipt['context'][-1]))
            references.append(toker.decode(ipt['response']))
            sample_ids.append(sample_id)
    
            if len(sample_ids) == infer_batch_size:
                yield prepare_infer_batch(features, toker), posts, references, sample_ids
                features = []
                sample_ids = []
                posts = []
                references = []

    if len(sample_ids) > 0:
        yield prepare_infer_batch(features, toker), posts, references, sample_ids
