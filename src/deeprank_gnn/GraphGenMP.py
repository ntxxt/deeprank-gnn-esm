import os
import sys
import glob
import h5py
import copy
from tqdm import tqdm
import time
import multiprocessing as mp
from functools import partial
import pickle
import torch
import warnings
from Bio.PDB.PDBParser import PDBParser
from Bio import BiopythonWarning

from .ResidueGraph import ResidueGraph
from .Graph import Graph

warnings.filterwarnings("ignore", category=BiopythonWarning)


class GraphHDF5(object):
    def __init__(
        self,
        pdb_path,
        ref_path=None,
        graph_type="residue",
        pssm_path=None,
        embedding_path=None,
        select=None,
        outfile="graph.hdf5",
        nproc=1,
        use_tqdm=True,
        tmpdir="./",
        limit=None,
        biopython=False,
    ):
        """Master class from which graphs are computed
        Args:
            pdb_path (str): path to the docking models
            ref_path (str, optional): path to the reference model. Defaults to None.
            graph_type (str, optional): Defaults to 'residue'.
            pssm_path ([type], optional): path to the pssm file. Defaults to None.
            embedding_path ([type], optional): path to the esm embedding file. Defaults to None.
            select (str, optional): filter files that starts with 'input'. Defaults to None.
            outfile (str, optional): Defaults to 'graph.hdf5'.
            nproc (int, optional): number of processors. Default to 1.
            use_tqdm (bool, optional): Default to True.
            tmpdir (str, optional): Default to `./`.
            limit (int, optional): Default to None.


            >>> pdb_path = './data/pdb/1ATN/'
            >>> embedding_path = './data/embedding/1ATN/'

            >>> GraphHDF5(pdb_path=pdb_path, embedding_path=embedding_path,
                          graph_type='residue', outfile='1AK4_residue.hdf5')
        """
        # get the list of PDB names
        pdbs = list(filter(lambda x: x.endswith(".pdb"), os.listdir(pdb_path)))
        if select is not None:
            pdbs = list(filter(lambda x: x.startswith(select), pdbs))

        # get the full path of the pdbs
        pdbs = [os.path.join(pdb_path, name) for name in pdbs]
        if limit is not None:
            if isinstance(limit, list):
                pdbs = pdbs[limit[0] : limit[1]]
            else:
                pdbs = pdbs[:limit]

        # get the pssm data
        pssm = {}
        for p in pdbs:
            base = os.path.basename(p)
            mol_name = os.path.splitext(base)[0]
            base_name = mol_name.split("_")[0]
            if pssm_path is not None:
                pssm[p] = self._get_pssm(pssm_path, mol_name, base_name)
            else:
                pssm[p] = None

        # get the ref path
        if ref_path is None:
            ref = None
        else:
            ref = os.path.join(ref_path, base_name + ".pdb")

        if embedding_path is None:
            raise ValueError("Please provide path to the embeddings")
        else:
            self.embedding_path = embedding_path

        # compute all the graphs on 1 core and directly
        # store the graphs the HDF5 file
        if nproc == 1:
            graphs = self.get_all_graphs(pdbs, pssm, ref, outfile, use_tqdm, biopython)

        else:

            if not os.path.isdir(tmpdir):
                os.mkdir(tmpdir)
            
            pool = mp.Pool(nproc)
            part_process = partial(
                self._pickle_one_graph, pssm=pssm, ref=ref, tmpdir=tmpdir, biopython=biopython)
            pool.map(part_process, pdbs) 

            # get graph names
            graph_names = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
            graph_names = list(filter(lambda x: x.endswith(".pkl"), graph_names))
            if select is not None:
                graph_names = list(
                    filter(lambda x: x.startswith(tmpdir + select), graph_names)
                )

            # transfer them to the hdf5
            with h5py.File(outfile, "w") as f5:
                desc = "{:25s}".format("   Store in HDF5")

                for name in graph_names:
                    f = open(name, "rb")
                    g = pickle.load(f)
                    try:
                        g.nx2h5(f5)
                    except Exception as e:
                        print("Issue encountered while computing graph ", name)
                        print(e)
                    f.close()
                    os.remove(name)
            print("Graphs added to the HDF5 file")

        # add embedding to the hdf5 file

        # clean up
        rmfiles = glob.glob("*.izone") + glob.glob("*.lzone") + glob.glob("*.refpairs")
        for f in rmfiles:
            os.remove(f)

        # add embedding to the hdf5 file
        self._add_embedding(outfile=outfile, pdbs=pdbs, embedding_path=embedding_path)


    def get_all_graphs(self, pdbs, pssm, ref, outfile, use_tqdm=True, biopython=False):

        graphs = []
        if use_tqdm:
            desc = "{:25s}".format("   Create HDF5")
            lst = tqdm(pdbs, desc=desc, file=sys.stdout)
        else:
            lst = pdbs

        for name in lst:
            try:
                graphs.append(self._get_one_graph(name, pssm, ref, biopython))
            except Exception as e:
                print("Issue encountered while computing graph ", name)
                print(e)

        with h5py.File(outfile, "w") as f5:
            for g in graphs:
                try:
                    g.nx2h5(f5)
                except Exception as e:
                    print("Issue encountered while storing graph ", g.pdb)
                    print(e)

    @staticmethod
    def _pickle_one_graph(name, pssm, ref, tmpdir="./", biopython=False):

        # get the graph
        try:

            g = ResidueGraph(pdb=name, pssm=pssm[name], biopython=biopython)

            if ref is not None:
                g.get_score(ref)

            # pickle it
            mol_name = os.path.basename(name)
            mol_name = os.path.splitext(mol_name)[0]
            fname = os.path.join(tmpdir, mol_name + ".pkl")

            f = open(fname, "wb")
            pickle.dump(g, f)
            f.close()

        except Exception as e:
            print("Issue encountered while storing graph ", name)
            print(e)

    @staticmethod
    def _get_one_graph(name, pssm, ref, biopython):

        # get the graph
        g = ResidueGraph(pdb=name, pssm=pssm[name], biopython=biopython)
        if ref is not None:
            g.get_score(ref)
        return g

    @staticmethod
    def _get_pssm(pssm_path, mol_name, base_name):

        if pssm_path is None:
            return None

        pssmA = os.path.join(pssm_path, base_name + ".A.pssm")
        pssmB = os.path.join(pssm_path, base_name + ".B.pssm")

        # check if the pssms exists
        if os.path.isfile(pssmA) and os.path.isfile(pssmB):
            pssm = {"A": pssmA, "B": pssmB}
        else:
            pssmA = os.path.join(pssm_path, base_name + ".A.pdb.pssm")
            pssmB = os.path.join(pssm_path, base_name + ".B.pdb.pssm")
            if os.path.isfile(pssmA) and os.path.isfile(pssmB):
                pssm = {"A": pssmA, "B": pssmB}
            else:
                pssmA = os.path.join(pssm_path, mol_name + ".A.pdb.pssm")
                pssmB = os.path.join(pssm_path, mol_name + ".B.pdb.pssm")
                if os.path.isfile(pssmA) and os.path.isfile(pssmB):
                    pssm = {"A": pssmA, "B": pssmB}
                else:
                    raise FileNotFoundError("PSSM file for " + mol_name + " not found")
        return pssm
    
    @staticmethod
    def _add_embedding(outfile, pdbs, embedding_path):
        f = h5py.File(outfile, 'r+')
        mols = f.keys()
        for mol in mols:
            residues = f[mol]['nodes'][()]
            embedding_tersor = torch.empty(len(residues), 1280)
            pdb_file = [i for i in pdbs if mol in i][0]
            for i in range(len(residues)):
                chainID = residues[i][0].decode()
                resID = residues[i][1].decode()
                pt_name = mol[:4] + '.' + chainID + '.pt'
                pt_path = os.path.join(embedding_path, pt_name)
                #res_number = int(resID) - GraphHDF5._get_starting_res(pdb_file, chainID) 
                res_number = int(resID)
                try:
                    embedding = torch.load(pt_path)["representations"][33][res_number - 1]
                    embedding_tersor[i] = embedding
                except:
                    embedding_tersor[i] = torch.zeros(1280)
            assert not torch.all(torch.eq(embedding_tersor, 0))
            f.create_dataset(f'/{mol}/node_data/embedding', data=embedding_tersor)
        print(f'Embedding added to the {outfile} file')
        f.close()

    '''
    @staticmethod
    def _get_starting_res(pdb_file, chainID):
        """Get the starting residue in a chain"""
        parser = PDBParser()
        structure = parser.get_structure("protein", pdb_file)
        chain = structure[0][chainID]
        start_res = next(chain.get_residues()).get_id()[1]
        return start_res'''
