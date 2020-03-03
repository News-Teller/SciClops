from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.decomposition import LatentDirichletAllocation
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.mixture import GaussianMixture
from sklearn.cluster import KMeans
from sklearn.metrics.cluster import v_measure_score
from torch import optim
import spacy

from gsdmm import MovieGroupProcess

############################### CONSTANTS ###############################
scilens_dir = str(Path.home()) + '/data/scilens/cache/diffusion_graph/scilens_3M/'
sciclops_dir = str(Path.home()) + '/data/sciclops/'
hn_vocabulary = open(sciclops_dir + 'small_files/hn_vocabulary/hn_vocabulary.txt').read().splitlines()

nlp = spacy.load('en_core_web_lg')
np.random.seed(42)
torch.manual_seed(42)
NUM_CLUSTERS = 20
############################### ######### ###############################

################################ HELPERS ################################

def load_matrices(representation, dimension=None):
	cooc = pd.read_csv(sciclops_dir + 'cache/cooc.tsv.bz2', sep='\t', index_col=['url', 'claim', 'popularity'])
	claims = pd.read_csv(sciclops_dir + 'cache/claims_'+representation+('_'+str(dimension) if dimension else '')+'.tsv.bz2', sep='\t', index_col=['url', 'claim', 'popularity'])
	papers = pd.read_csv(sciclops_dir + 'cache/papers_'+representation+('_'+str(dimension) if dimension else '')+'.tsv.bz2', sep='\t', index_col=['url', 'title', 'popularity'])
	return cooc, papers, claims


# Hyper Parameters
num_epochs = 400
learning_rate = 1.e-3
hidden = 50
batch_size = 64
gamma = 1.e-3

class SingleClusterNet(nn.Module):
	def __init__(self, clustering_type, init_clustering_method):
		super(SingleClusterNet, self).__init__()
		
		self.clustering_type = clustering_type

		if 'compute_C' in self.clustering_type:
			self.papers, _, papers_clusters, claims_clusters, self.cooc = standalone_clustering(method=init_clustering_method)
			
			self.papers_index = papers_clusters.index
			self.claims_index = claims_clusters.index
			self.claims_clusters = claims_clusters.values
			self.cooc_unique, index = np.unique(self.cooc, axis=0, return_index=True)
			self.claims_unique = self.claims_clusters[index]

			self.cooc_unique = torch.Tensor(self.cooc_unique.astype(float))
			self.papers = torch.Tensor(self.papers.astype(float))
			self.claims_unique = torch.Tensor(self.claims_unique.astype(float))

			if 'transform_P' in self.clustering_type:
				self.papersNet = nn.Sequential(
					nn.Linear(self.papers.shape[1], hidden),
					nn.BatchNorm1d(hidden),
					nn.ReLU(),
					nn.Linear(hidden, NUM_CLUSTERS),
					nn.Softmax(dim=1)
				)
			elif 'align_P' in self.clustering_type:
				self.papers_clusters = nn.Parameter(nn.init.eye_(torch.Tensor(self.papers.shape[0], NUM_CLUSTERS)), requires_grad=True)

		elif 'compute_P' in self.clustering_type:
			_, self.claims, papers_clusters, claims_clusters, self.cooc = standalone_clustering(method=init_clustering_method)

			self.papers_index = papers_clusters.index
			self.claims_index = claims_clusters.index
			self.papers_clusters = papers_clusters.values
			self.cooc_unique, index = np.unique(self.cooc, axis=1, return_index=True)
			self.papers_unique = self.papers_clusters[index]

			self.cooc_unique = torch.Tensor(self.cooc_unique.astype(float))
			self.claims = torch.Tensor(self.claims.astype(float))
			self.papers_unique = torch.Tensor(self.papers_unique.astype(float))

			if 'transform_C' in self.clustering_type:
				self.claimsNet = nn.Sequential(
					nn.Linear(self.claims.shape[1], hidden),
					nn.BatchNorm1d(hidden),
					nn.ReLU(),
					nn.Linear(hidden, NUM_CLUSTERS),
					nn.Softmax(dim=1)
				)
			elif 'align_C' in self.clustering_type:
				self.claims_clusters = nn.Parameter(nn.init.eye_(torch.Tensor(self.claims.shape[0], NUM_CLUSTERS)), requires_grad=True)

	def compute_permutation(self, _):
		if 'transform_P' in self.clustering_type or 'align_P' in self.clustering_type: 
			self.permutation = np.random.permutation(len(self.papers))	
		elif 'transform_C' in self.clustering_type or 'align_C' in self.clustering_type:
			self.permutation = np.random.permutation(len(self.claims))	
		return self.permutation

	def forward(self, batch, _):
		if 'compute_C' in self.clustering_type:
			L = self.cooc_unique[:, self.permutation[batch:batch+batch_size]]
			C = self.claims_unique

			if 'transform_P' in self.clustering_type:
				P = self.papersNet(self.papers[self.permutation[batch:batch+batch_size]])
			elif 'align_P' in self.clustering_type:
				P = self.papers_clusters[self.permutation[batch:batch+batch_size]]

		elif 'compute_P' in self.clustering_type:
			L = self.cooc_unique[self.permutation[batch:batch+batch_size]]
			P = self.papers_unique

			if 'transform_C' in self.clustering_type:
				C = self.claimsNet(self.claims[self.permutation[batch:batch+batch_size]])
			elif 'align_C' in self.clustering_type:
				C = self.claims_clusters[self.permutation[batch:batch+batch_size]]

		return P, L, C

	def final_clusters(self):
		if 'compute_C' in self.clustering_type:
			claims_clusters, cooc = self.claims_clusters, self.cooc
			if 'transform_P' in self.clustering_type:
				papers_clusters = self.papersNet(self.papers).detach().numpy()
			elif 'align_P' in self.clustering_type:
				papers_clusters = self.papers_clusters.detach().numpy()

		elif 'compute_P' in self.clustering_type:
			papers_clusters, cooc = self.papers_clusters, self.cooc
			if 'transform_C' in self.clustering_type:
				claims_clusters = self.claimsNet(self.claims).detach().numpy()
			elif 'align_C' in self.clustering_type:
				claims_clusters = self.claims_clusters.detach().numpy()
		
		papers_clusters = pd.DataFrame(papers_clusters, index=self.papers_index)
		claims_clusters = pd.DataFrame(claims_clusters, index=self.claims_index)

		return papers_clusters, claims_clusters, cooc

	def loss(self, P, L, C):
		C_prime = L @ P
		return torch.norm(C_prime - C, p='fro') - gamma * (torch.norm(P, p='fro') + torch.norm(C, p='fro'))


class CoordinateClusterNet(nn.Module):
	def __init__(self, clustering_type, init_clustering_method):
		super(CoordinateClusterNet, self).__init__()
		
		self.clustering_type = clustering_type

		self.papers, self.claims, papers_clusters, claims_clusters, self.cooc = standalone_clustering(method=init_clustering_method)
		
		self.papers_index = papers_clusters.index
		self.claims_index = claims_clusters.index		
		self.cooc_unique_C, self.index_C = np.unique(self.cooc, axis=0, return_index=True)
		self.cooc_unique_P, self.index_P = np.unique(self.cooc, axis=1, return_index=True)

		self.cooc_unique_C = torch.Tensor(self.cooc_unique_C.astype(float))
		self.papers = torch.Tensor(self.papers.astype(float))
		self.cooc_unique_P = torch.Tensor(self.cooc_unique_P.astype(float))
		self.claims = torch.Tensor(self.claims.astype(float))

		if 'coordinate-transform' in self.clustering_type:
			self.claimsNet = nn.Sequential(
				nn.Linear(self.claims.shape[1], hidden),
				nn.ReLU(),
				nn.Linear(hidden, NUM_CLUSTERS),
				nn.Softmax(dim=1)
			)
			self.papersNet = nn.Sequential(
				nn.Linear(self.papers.shape[1], hidden),
				nn.ReLU(),
				nn.Linear(hidden, NUM_CLUSTERS),
				nn.Softmax(dim=1)
			)
			
		elif 'coordinate-align' in self.clustering_type:
			self.claims_clusters = nn.Parameter(nn.init.eye_(torch.Tensor(self.claims.shape[0], NUM_CLUSTERS)), requires_grad=True)
			self.papers_clusters = nn.Parameter(nn.init.eye_(torch.Tensor(self.papers.shape[0], NUM_CLUSTERS)), requires_grad=True)

		elif 'compute-align' in self.clustering_type:
			self.papers_clusters = nn.Parameter(torch.Tensor(papers_clusters.values.astype(float)), requires_grad=True)
			self.claims_clusters = nn.Parameter(torch.Tensor(claims_clusters.values.astype(float)), requires_grad=True)

	def compute_permutation(self, epoch): 
		self.permutation = np.random.permutation(len(self.papers)) if epoch%2==0 else np.random.permutation(len(self.claims))
		return self.permutation

	def forward(self, batch, epoch):
		if epoch%2==0:
			L = self.cooc_unique_C[:, self.permutation[batch:batch+batch_size]]
			
			if 'coordinate-align' in self.clustering_type or 'compute-align' in self.clustering_type:
				C = torch.Tensor(self.claims_clusters[self.index_C].detach().numpy().astype(float))
				P = self.papers_clusters[self.permutation[batch:batch+batch_size]]
			elif 'coordinate-transform' in self.clustering_type:
				C = torch.Tensor(self.claimsNet(self.claims[self.index_C]).detach().numpy().astype(float))
				P = self.papersNet(self.papers[self.permutation[batch:batch+batch_size]])
		else:
			L = self.cooc_unique_P[self.permutation[batch:batch+batch_size]]
			
			if 'coordinate-align' in self.clustering_type or 'compute-align' in self.clustering_type:
				P = torch.Tensor(self.papers_clusters[self.index_P].detach().numpy().astype(float))
				C = self.claims_clusters[self.permutation[batch:batch+batch_size]]
			elif 'coordinate-transform' in self.clustering_type:
				P = torch.Tensor(self.papersNet(self.papers[self.index_P]).detach().numpy().astype(float))
				C = self.claimsNet(self.claims[self.permutation[batch:batch+batch_size]])

		return P, L, C

	def final_clusters(self):
		cooc = self.cooc
		if 'coordinate-align' in self.clustering_type or 'compute-align' in self.clustering_type:
			papers_clusters = self.papers_clusters.detach().numpy()
			claims_clusters = self.claims_clusters.detach().numpy()
		elif 'coordinate-transform' in self.clustering_type:
			papers_clusters = self.papersNet(self.papers).detach().numpy()
			claims_clusters = self.claimsNet(self.claims).detach().numpy()

		papers_clusters = pd.DataFrame(papers_clusters, index=self.papers_index)
		claims_clusters = pd.DataFrame(claims_clusters, index=self.claims_index)

		return papers_clusters, claims_clusters, cooc

	def loss(self, P, L, C):
		C_prime = L @ P
		return torch.norm(C_prime - C, p='fro') - gamma * (torch.norm(P, p='fro') + torch.norm(C, p='fro'))

############################### ######### ###############################

def eval_clusters(papers_clusters, claims_clusters, cooc):
	#papers_clusters, claims_clusters, cooc = align_clustering('compute-align', 'PCA-GMM')

	papers_index = papers_clusters.index
	claims_index = claims_clusters.index
	papers_clusters = papers_clusters.values
	claims_clusters = claims_clusters.values

	# V-Measure
	threshold = .0
	top_papers = np.any(papers_clusters > threshold, axis=1)

	P = papers_clusters[top_papers]
	L = cooc[:, top_papers]
	mask = (~np.all(L == 0, axis=1))
	L = L[mask]
	C = claims_clusters[mask]
	
	labels_inherited = np.multiply(L, np.argmax(P, axis=1)).max(axis=1)
	labels_expected = np.argmax(C, axis=1)

	v1 = v_measure_score(labels_expected, labels_inherited)

	top_claims = np.any(claims_clusters > threshold, axis=1)

	C = claims_clusters[top_claims]
	L = cooc[top_claims]
	mask = (~np.all(L == 0, axis=0))
	L = L[:, mask]
	P = papers_clusters[mask]
	
	labels_inherited = np.multiply(L.T, np.argmax(C, axis=1)).max(axis=1)
	labels_expected = np.argmax(P, axis=1)

	v2 = v_measure_score(labels_expected, labels_inherited)
	v = np.mean([v1, v2])

	# #STS
	papers_clusters = pd.DataFrame(papers_clusters, index=papers_index)[top_papers]
	papers_clusters_repr = papers_clusters.reset_index(['url', 'popularity'], drop=True).idxmax().reset_index().rename(columns={'index':'cluster', 0:'title'})
	papers_clusters = papers_clusters.idxmax(axis=1)
	papers_clusters = papers_clusters.reset_index().drop(['url', 'popularity'], axis=1).rename(columns={0:'cluster'})

	claims_clusters = pd.DataFrame(claims_clusters, index=claims_index)[top_claims]
	claims_clusters_repr = claims_clusters.reset_index(['url', 'popularity'], drop=True).idxmax().reset_index().rename(columns={'index':'cluster', 0:'claim'})
	claims_clusters = claims_clusters.idxmax(axis=1)
	claims_clusters = claims_clusters.reset_index().drop(['url', 'popularity'], axis=1).rename(columns={0:'cluster'})

	def compute_sts(text_1, text_2):
		semantic = nlp(text_1).similarity(nlp(text_2))
		# text_1 = set(text_1.split()).intersection(hn_vocabulary)
		# text_2 = set(text_2.split()).intersection(hn_vocabulary)
		#jaccard = len(text_1.intersection(text_2)) / (len(text_1.union(text_2)) or 1)
		return semantic#np.mean([semantic,jaccard])

	papers = papers_clusters.merge(claims_clusters_repr)
	papers['sim'] = papers.apply(lambda p: compute_sts(p.claim, p.title), axis=1)
	mean_pc = papers.groupby('cluster')['sim'].median().mean()

	claims = claims_clusters.merge(papers_clusters_repr)
	claims['sim'] = claims.apply(lambda p: compute_sts(p.claim, p.title), axis=1)
	mean_cp = claims.groupby('cluster')['sim'].median().mean()

	# papers = papers_clusters.merge(papers_clusters_repr, on='cluster')
	# mean_pp = papers.apply(lambda p: compute_sts(p.title_x, p.title_y), axis=1).median()

	# claims = claims_clusters.merge(claims_clusters_repr, on='cluster')
	# mean_cc = claims.apply(lambda p: compute_sts(p.claim_x, p.claim_y), axis=1).median()


	sts = np.mean([mean_cp, mean_pc])
		
	return v, sts

def standalone_clustering(method):
	dimension = 10 if method.startswith('PCA') else None

	if method.endswith('GMM'):
		cooc, papers, claims = load_matrices(representation='embeddings', dimension=dimension)

		cooc = cooc.values
		papers_index = papers.index
		claims_index = claims.index
		papers = papers.values
		claims = claims.values
		
		model = GaussianMixture(NUM_CLUSTERS, covariance_type='spherical', tol=0.5, random_state=42).fit(np.concatenate([claims, papers]))
		claims_clusters = model.predict_proba(claims)
		papers_clusters = model.predict_proba(papers)
		
	elif method.endswith('KMeans'):
		cooc, papers, claims = load_matrices(representation='embeddings', dimension=dimension)

		cooc = cooc.values
		papers_index = papers.index
		claims_index = claims.index		
		papers = papers.values
		claims = claims.values
		
		model = KMeans(NUM_CLUSTERS, random_state=42).fit(np.concatenate([claims, papers]))
		c_cluster = model.predict(claims)
		p_cluster = model.predict(papers)

		claims_clusters = np.zeros((len(claims), NUM_CLUSTERS))
		claims_clusters[np.arange(len(claims)), c_cluster] = 1
		papers_clusters = np.zeros((len(papers), NUM_CLUSTERS))
		papers_clusters[np.arange(len(papers)), p_cluster] = 1

	elif method == 'LDA':
		cooc, papers, claims = load_matrices(representation='textual')
		cooc = cooc.values
		papers_index = papers.index
		claims_index = claims.index
		papers = papers['clean_passage']
		claims = claims['clean_claim']
		
		CV = CountVectorizer().fit(pd.concat([claims, papers]))

		model = LatentDirichletAllocation(n_components=NUM_CLUSTERS, n_jobs=-1).fit(CV.transform(pd.concat([claims, papers])))
		papers_clusters = model.transform(CV.transform(papers))
		claims_clusters = model.transform(CV.transform(claims))

	elif method == 'GSDMM':
		cooc, papers, claims = load_matrices(representation='textual')
		cooc = cooc.values
		papers_index = papers.index
		claims_index = claims.index
		
		c_cluster = MovieGroupProcess(K=NUM_CLUSTERS, n_iters=5).fit(claims['clean_claim'], len(set([e for l in claims['clean_claim'].tolist() for e in l])))
		p_cluster = MovieGroupProcess(K=NUM_CLUSTERS, n_iters=5).fit(papers['clean_passage'], len(set([e for l in papers['clean_passage'].tolist() for e in l])))
		
		claims_clusters = np.zeros((len(claims), NUM_CLUSTERS))
		claims_clusters[np.arange(len(claims)), c_cluster] = 1
		papers_clusters = np.zeros((len(papers), NUM_CLUSTERS))
		papers_clusters[np.arange(len(papers)), p_cluster] = 1

	papers_clusters = pd.DataFrame(papers_clusters, index=papers_index)
	claims_clusters = pd.DataFrame(claims_clusters, index=claims_index)

	return papers, claims, papers_clusters, claims_clusters, cooc
	

def align_clustering(clustering_type, init_clustering_method):

	#Model training
	if clustering_type in ['compute_C_transform_P', 'compute_C_align_P', 'compute_P_transform_C', 'compute_P_align_C']:
		model = SingleClusterNet(clustering_type, init_clustering_method)
	elif clustering_type in ['coordinate-transform', 'coordinate-align', 'compute-align']:
		model = CoordinateClusterNet(clustering_type, init_clustering_method)
	optimizer = optim.Adam(model.parameters(), lr=learning_rate) 

	for epoch in range(num_epochs):
		permutation = model.compute_permutation(epoch)

		mean_loss = []
		for batch in range(0, len(permutation), batch_size):
			optimizer.zero_grad()
			P, L, C = model.forward(batch, epoch)
			loss = model.loss(P, L, C)
			mean_loss.append(loss.detach().numpy())
			loss.backward()
			optimizer.step()

		# if epoch%1 == 0:
		# 	print(sum(mean_loss)/len(mean_loss))

	papers_clusters, claims_clusters, cooc = model.final_clusters()
	return papers_clusters, claims_clusters, cooc


#def popularity_filtering():

# def popularity_clustering(learn_transform, iterations=1, top_k=5):
	
# 	prior = [1/NUM_CLUSTERS for _ in range(NUM_CLUSTERS)]
	
# 	for _ in range(iterations):
# 		papers, claims = align_clustering(prior, learn_transform, top_k)
		
# 		popularity = claims.reset_index('popularity')['popularity']
# 		prior = [sum(claims[i]*popularity) for i in range(NUM_CLUSTERS)]
# 		prior = [p/sum(prior) for p in prior]

# 	
# 	claims.to_csv(sciclops_dir + 'cache/claims_clusters.tsv.bz2', sep='\t')


if __name__ == "__main__":
	compare = True
	if compare:
		
		# results = {}
		# for clustering_type in ['LDA', 'GSDMM', 'GMM', 'PCA-GMM', 'KMeans', 'PCA-KMeans']:
		# 	if clustering_type == 'GSDMM':
		# 		continue
		# 	_, _, papers_clusters, claims_clusters, cooc = standalone_clustering(clustering_type)
		# 	v, sts = eval_clusters(papers_clusters, claims_clusters, cooc)
		# 	results[clustering_type] = (v, sts)
		# print(results)

		# results = {}
		# for clustering_type in ['compute_C_transform_P', 'compute_C_align_P', 'compute_P_transform_C', 'compute_P_align_C']:
		# 	papers_clusters, claims_clusters, cooc = align_clustering(clustering_type, 'PCA-GMM')
		# 	v, sts = eval_clusters(papers_clusters, claims_clusters, cooc)
		# 	results[clustering_type] = (v, sts)
		# print(results)

		results = {}
		for clustering_type in ['compute-align', 'coordinate-transform', 'coordinate-align']:
			papers_clusters, claims_clusters, cooc = align_clustering(clustering_type, 'PCA-GMM')
			v, sts = eval_clusters(papers_clusters, claims_clusters, cooc)
			results[clustering_type] = (v, sts)
		print(results)
	else:
		papers_clusters, claims_clusters, _ = align_clustering('compute-align', 'PCA-GMM')
		papers_clusters.to_csv(sciclops_dir + 'cache/papers_clusters.tsv.bz2', sep='\t')
		claims_clusters.to_csv(sciclops_dir + 'cache/claims_clusters.tsv.bz2', sep='\t')
