# vi: tabstop=4 expandtab shiftwidth=4 softtabstop=4 mouse=a hlsearch
import os 
import re
from Masking import masking as masking
from Masking import surf_masking
from MRI import normalize
from Registration.ants_mri_normalize import APPIANApplyTransforms, APPIANConcatenateTransforms, APPIANRegistration
from arg_parser import icbm_default_brain_mask
import nipype.interfaces.minc as minc
import Initialization.initialization as init
import Partial_Volume_Correction.pvc as pvc 
import Results_Report.results as results
import Quantification.quantification as quant
import Quality_Control.qc as qc
import Quality_Control.dashboard as dash
import Test.test_group_qc as tqc
import nipype.interfaces.utility as niu
import nipype.pipeline.engine as pe
import nipype.interfaces.io as nio
import nipype.interfaces.utility as util

def pexit(output_string, errorcode=1) : 
    print(output_string)
    exit(errorcode)

class Workflows:
    def __init__(self, opts) :
        # Create lists to store main output images
        self.out_node_list = [] 
        self.out_img_list = []
        self.out_img_dim = []
        self.extract_values = []
        self.datasink_dir_name = []
        
        # Create Nipype workflow
        self.workflow = pe.Workflow(name=opts.preproc_dir)
        self.workflow.base_dir = opts.targetDir

        #Tuples of 3 elements represent 
        # 1) function to initialize a workflow 
        # 2) possible flag to start running APPIAN after workflow, ignoring the rest
        # 3) flag that signals whether to run workflow

        # ___WARNING___: Do not change order of this tuple! Workflows must be initialized in specific sequence
        self.init_functions = ( (self.set_preinfosource, False, True ),
        (self.set_infosource, False, True),
        (self.set_datasource_pet, False, True),
        (self.set_datasource_anat, False, True),
        (self.set_datasource_surf, False, opts.use_surfaces),
        (self.set_datasource_base, opts.datasource_exit, True),
        (self.set_init_pet, opts.initialize_exit, True ),
        (self.set_mri_preprocess, opts.mri_preprocess_exit, True),
        (self.set_template_normalization, False, True),
        (self.set_pet2mri, opts.coregistration_exit, True),
        (self.set_masking, opts.masking_exit, True),
        (self.set_pvc, opts.pvc_exit, opts.pvc_method),
        (self.set_quant, False, opts.quant_method ),
        (self.set_datasink, False, True),
        (self.set_results_report, False, not opts.no_results_report ),
        (self.set_results_report_surf, False, opts.use_surfaces ),
        (self.set_qc_metrics, False, False), 
        (self.set_dashboard, False, False)
        )

    def initialize(self, opts) :
        '''
        This is the main function for setting up and then running the scanLevel analysis. 
        It first uses <preinfosource> to identify which scans exist for the which combination of  task, 
        session, and subject IDs. This is stored in <valid_args>, which is then passed to inforsource.

        Infosource iterates over the valid subjects and uses DataGrabber to find the input files for 
        each of these subjects. Depdning on the user-options defined in <opts>, for each scan PET-T1 
        co-registration, partial-volume correction, tracer kinetic analysis and results reporting 
        are performed. 

        This function initializes the workflows that make up APPIAN. 
        Not all of them always have to be run, so there is a <run_flag> variable to signal 
        which ones need to be run. For debugging purposes it may be useful not to create the
        full APPIAN self.workflow. In this case some of the workflows have <return_early_flag> variables
        that can be set by the user when launching APPIAN and will result in APPIAN not initialzing
        any subsequent workflows'''
        
        if opts.verbose >= 2 :
            print("\n\nWorkflow Initialization: ")
        for set_workflow_function, return_early_flag, run_flag in self.init_functions :
            #TODO : Use line below with verbose option
            if run_flag != None and run_flag != False :
                if opts.verbose >= 2 :
                    print("\t",set_workflow_function, return_early_flag, run_flag )
                set_workflow_function( opts)
                if return_early_flag :
                    return(0)

    ##########################
    # Template normalization #
    ##########################
    def set_template_normalization(self, opts):

        #Setup node for nonlinear alignment of results template to default (icbm152) template
        self.pvc_template_normalization = pe.Node(interface=APPIANRegistration(), name="pvc_template_normalizationalization")
        self.quant_template_normalization = pe.Node(interface=APPIANRegistration(), name="quant_template_normalizationalization")
        self.results_template_normalization = pe.Node(interface=APPIANRegistration(), name="results_template_normalizationalization")

        #name of label type
        names=["quant", "pvc", "results"]
        #type of label
        types=[opts.quant_label_type, opts.pvc_label_type, opts.results_label_type]
        #nipype nodes that need to be initialized with appropriate parameters
        nodes=[self.quant_template_normalization, self.pvc_template_normalization, self.results_template_normalization]
        #user defined template images that will be normalized to standard stereotaxic template
        moving_images=[opts.quant_label_template, opts.pvc_label_template, opts.results_label_template]

        for label_name, label_type, node, moving_image in zip(names, types, nodes, moving_images ) : 
            if label_type == "atlas-template" :
                print(label_name + "_template_normalization")
                print(label_name, label_type, moving_image)
                node.inputs.fixed_image_space='stx'
                node.inputs.moving_image_space='template'
                node.inputs.interpolation='Linear'
                node.inputs.normalization_type = 'lin' #opts.normalization_type
                node.inputs.moving_image = moving_image
                node.inputs.fixed_image = opts.template 
                node.inputs.fixed_image_mask = opts.template_brain_mask 
                if opts.user_ants_command != None :
                    node.inputs.user_ants_command = opts.user_ants_command

                self.out_node_list += [node]
                self.out_img_list += ['warped_image']
                self.out_img_dim += ['3']
                self.extract_values += [False]
                self.datasink_dir_name += ['template_normalization/'+label_name]

    ######################
    # PET Initialization #
    ######################
    def set_init_pet(self, opts):
        self.init_pet=init.get_workflow("initialization", self.infosource, opts)
        self.workflow.connect(self.datasource, 'pet', self.init_pet, "inputnode.pet")
        self.workflow.connect(self.datasource, 'json_header', self.init_pet, "inputnode.pet_header_json")
    
    #####################
    # MRI Preprocessing # 
    #####################
    def set_mri_preprocess(self, opts) :
        if opts.pet_coregistration_target == 't1' :
            self.mri_preprocess = normalize.get_workflow("mri", opts)
            #If user wants to input their own brain mask with the option --user-brainmask,
            #then the source node for the brain mask is datasource. Otherwise it is derived in 
            if opts.user_brainmask : 
                self.brain_mask_space_stx_node = self.datasourceAnat
                self.brain_mask_space_stx_file = 'brain_mask_space_stx'
                self.workflow.connect(self.datasource, 'brain_mask_space_stx', self.mri_preprocess, 'inputnode.brain_mask_space_stx') 
            else : 
                self.brain_mask_space_stx_node = self.mri_preprocess
                self.brain_mask_space_stx_file='outputnode.brain_mask_space_stx'

            self.mri_space_nat_name = 'outputnode.mri_space_nat'
            self.brain_mask_space_mri_name = 'outputnode.brain_mask_space_mri'
            self.brain_mask_space_stx_name='outputnode.brain_mask_space_stx'

            self.out_node_list += [self.brain_mask_space_stx_node, self.mri_preprocess, self.mri_preprocess] 
            self.out_img_list += [self.brain_mask_space_stx_file, 'mri_spatial_normalized.warped_image','mri_spatial_normalized.inverse_warped_image']
            self.out_img_dim += ['3','3','3']
            self.extract_values += [False,False,False]
            self.datasink_dir_name += ['mri/mri_brain_mask', 'mri/mri2stx','mri/stx2mri']

            #If user wants to input their own mri space to mni space transform with the option --user-mrimni,
            #then the source node for the brain mask is datasource. Otherwise it is derived in 
            #stereotaxic space in self.mri_preprocess
            if opts.user_mri_stx != '' : 
                #USER PROVIDED TFM
                self.tfm_node = self.datasource
                self.mri_stx_tfm = 'tfm_mri_stx'
                self.stx_mri_tfm = 'tfm_stx_mri'
                self.workflow.connect(self.datasourceAnat, 'tfm_mri_stx', self.mri_preprocess, 'inputnode.tfm_mri_stx') 
                self.workflow.connect(self.datasourceAnat, 'tfm_stx_mri', self.mri_preprocess, 'inputnode.tfm_stx_mri') 
                self.out_img_list += ["transform_mri.output_image"]
                self.mri_space_stx_name="transform_mri.output_image"
            else : 
                #ANTS TFM
                self.tfm_node = self.mri_preprocess
                self.mri_stx_tfm='outputnode.tfm_mri_stx'       
                self.stx_mri_tfm='outputnode.tfm_stx_mri'       
                
                self.out_img_list += [ "mri_spatial_normalized.warped_image" ]
                self.mri_space_stx_name = "mri_spatial_normalized.warped_image" 
            self.workflow.connect(self.datasourceAnat,'mri',self.mri_preprocess, 'inputnode.mri')   


            self.out_node_list += [self.mri_preprocess] 
            self.out_img_dim += ['3']
            self.extract_values += [False]
            self.datasink_dir_name += ['t1/stereotaxic']
        else : 

            self.mri_preprocess = pe.Node(niu.IdentityInterface(fields=['brain_mask_space_stx', 'brain_mask_space_mri', 'mri_space_stx', 'mri_space_nat' ]), name="mni_template" )
            self.mri_preprocess.inputs.brain_mask_space_stx = icbm_default_brain_mask
            self.mri_preprocess.inputs.brain_mask_space_mri = icbm_default_brain_mask
            self.mri_preprocess.inputs.mri_space_stx = opts.template
            self.mri_preprocess.inputs.mri_space_nat = opts.template
    
            self.mri_space_nat_name = 'mri_space_nat'
            self.brain_mask_space_mri_name = 'brain_mask_space_mri'

            self.mri_space_stx_name = "mri_space_stx" 
            self.brain_mask_space_stx_name='brain_mask_space_stx'
            self.brain_mask_space_stx_node = self.mri_preprocess
    #############################
    # PET-to-MRI Coregistration #
    #############################
    def set_pet2mri(self, opts) :
        self.pet2mri = pe.Node(interface=APPIANRegistration(), name="pet2mri")
        self.pet2mri.inputs.moving_image_space='pet'
        if opts.pet_coregistration_target == "t1":
            self.pet2mri.inputs.fixed_image_space='T1w'
            self.pet2mri.inputs.normalization_type='rigid'
        elif opts.pet_coregistration_target == "stx":
            self.pet2mri.inputs.fixed_image_space='stx'
            self.pet2mri.inputs.normalization_type='affine'
        else :
            pexit("Error: PET coregistration target not implemented "+opts.pet_coregistration_target+"\nMust be either \'t1\' or \'stx\'")
        
        if opts.pet_brain_mask:
            workflow.connect(self.init_pet, 'pet_brain_mask',self.pet2mri, 'moving_image_mask')
        
        if opts.pet_coregistration_target == "t1":
            self.workflow.connect(self.datasource, 'mri', self.pet2mri, 'fixed_image')
            self.workflow.connect(self.mri_preprocess,self.brain_mask_space_mri_name, self.pet2mri, 'fixed_image_mask')
        elif opts.pet_coregistration_target == "stx":
            self.pet2mri.inputs.fixed_image =  opts.template
            self.pet2mri.inputs.fixed_image_mask = opts.template_brain_mask
        else :
            pexit("Error: PET coregistration target not implemented "+opts.pet_coregistration_target+"\nMust be either \'t1\' or \'stx\'")
            
        self.workflow.connect(self.init_pet, 'outputnode.pet_volume', self.pet2mri, 'moving_image')

        #If analysis_space != pet, then resample 4d PET image to T1 or stereotaxic space
        if opts.analysis_space in ['t1', 'stereo'] :
            pet_analysis_space = pe.Node(interface=APPIANApplyTransforms(), name='pet_space_mri')
            self.workflow.connect(self.datasource, 'pet', pet_analysis_space, 'input_image')
            self.pet_input_node=pet_analysis_space
            self.pet_input_file='output_image'
            if opts.analysis_space == 't1' and opts.pet_coregistration_target == 't1' :
                self.workflow.connect(self.mri_preprocess, self.mri_space_nat_name, pet_analysis_space, 'reference_image')
                self.workflow.connect(self.pet2mri, 'out_matrix', pet_analysis_space, 'transform_2')
            else : 
                #Resample 4d PET image to MNI space
                self.workflow.connect(self.pet2mri, "out_matrix", pet_analysis_space, 'transform_1')
                self.workflow.connect(self.tfm_node, self.mri_stx_tfm, pet_analysis_space, 'transform_2')
                self.workflow.connect(self.mri_preprocess, self.mri_space_stx_name,  pet_analysis_space, 'reference_image')

        else : 
            self.pet_input_node=self.datasource
            self.pet_input_file='pet'

        #if opts.test_group_qc :
        #    self.misregistration = pe.Node(interface=niu.IdentityInterface(fields=['error']), name="misregistration")
        #    self.misregistration.iterables = ('error',tqc.errors)
        #    self.workflow.connect(self.misregistration, 'error', self.pet2mri, "inputnode.error")
        #Add the outputs of Coregistration to list that keeps track of the outputnodes, images, 
        # and the number of dimensions of these images       
        self.out_node_list += [self.pet_input_node, self.pet2mri] 
        self.out_img_list += [self.pet_input_file, 'inverse_warped_image']
        self.out_img_dim += ['4', '3']
        self.extract_values += [True, False]
        self.datasink_dir_name += ['pet_coregistration', 't1']

    ###########
    # Masking #
    ###########
    def set_masking(self, opts) :
        #
        # Set the appropriate nodes and inputs for desired "analysis_level" 
        # and for the source for the labels                                 
        #
        
        self.masking=masking.get_workflow("masking", self.infosource, opts)
        if opts.quant_label_type in ['atlas', 'atlas-template', 'user_cls'] or opts.pet_coregistration_target == 'stx' :
            self.quant_label_node = self.datasource
            self.quant_label_file = 'quant_label_img'
        elif opts.quant_label_type == 'internal_cls' :
            self.quant_label_node = self.mri_preprocess
            self.quant_label_file = 'outputnode.quant_label_img'
        else :
            print("Error: pvc_label_type is not valid:", opts.pvc_label_type)
            exit(1)

        if opts.pvc_label_type in ['atlas', 'atlas-template', 'user_cls'] :
            self.pvc_label_node = self.datasource
            self.pvc_label_file = 'pvc_label_img'
        elif opts.pvc_label_type == 'internal_cls' :
            self.pvc_label_node = self.mri_preprocess
            self.pvc_label_file = 'outputnode.pvc_label_img'
        else :
            print("Error: pvc_label_type is not valid:", opts.pvc_label_type)
            exit(1)

        if opts.results_label_type in [ 'atlas', 'atlas-template', 'user_cls'] :
            self.results_label_node = self.datasource
            self.results_label_file = 'results_label_img'
        elif opts.results_label_type == 'internal_cls' :
            self.results_label_node = self.mri_preprocess
            self.results_label_file = 'outputnode.results_label_img'
        else :
            print("Error: results_label_type is not valid:", opts.pvc_label_type)
            exit(1)

        #If labels are being transformed from a non-standard template, pass the transform from the template coordinate
        #space to the standard (MNI152) coordinate space.
        if opts.pvc_label_type == 'atlas-template' :
            self.workflow.connect(self.pvc_template_normalization, 'composite_transform', self.masking, 'inputnode.tfm_pvc_tmp_stx' )

        if opts.quant_label_type == 'atlas-template':
            self.workflow.connect(self.quant_template_normalization, 'composite_transform', self.masking, 'inputnode.tfm_quant_tmp_stx' )
        
        if opts.results_label_type == 'atlas-template':
            self.workflow.connect(self.results_template_normalization, 'composite_transform', self.masking, 'inputnode.tfm_results_tmp_stx' )
      


        self.workflow.connect(self.mri_preprocess, self.mri_space_nat_name, self.masking, "inputnode.mri_space_nat")
        self.workflow.connect(self.mri_preprocess, self.mri_space_stx_name, self.masking, "inputnode.mri_space_stx")
        self.workflow.connect(self.mri_preprocess, self.brain_mask_space_mri_name, self.masking, "inputnode.brain_mask_space_mri")
        
        if opts.pet_coregistration_target == 't1' :
            self.workflow.connect(self.tfm_node, self.mri_stx_tfm, self.masking, "inputnode.tfm_mri_stx")
            self.workflow.connect(self.tfm_node, self.stx_mri_tfm, self.masking, "inputnode.tfm_stx_mri")
        self.workflow.connect(self.init_pet, 'outputnode.pet_header_json', self.masking, 'inputnode.pet_header_json')

        self.workflow.connect(self.pet2mri, "out_matrix", self.masking, "inputnode.tfm_pet_struct")
        self.workflow.connect(self.pet2mri, "out_matrix_inverse", self.masking, "inputnode.tfm_struct_pet")
        
        self.workflow.connect(self.brain_mask_space_stx_node, self.brain_mask_space_stx_name, self.masking, "inputnode.brain_mask_space_stx")
        if opts.pvc_method != None:
            #If PVC method has been set, define binary masks to contrain PVC
            self.workflow.connect(self.preinfosource, 'pvc_labels', self.masking, "inputnode.pvc_labels")
            self.workflow.connect(self.pvc_label_node, self.pvc_label_file, self.masking, "inputnode.pvc_label_img")
        if opts.quant_method != None :
            #If TKA method has been set, define binary masks for reference region
            self.workflow.connect(self.preinfosource, 'quant_labels', self.masking, "inputnode.quant_labels")
            self.workflow.connect(self.quant_label_node, self.quant_label_file, self.masking, "inputnode.quant_label_img")

        #Results labels are always set
        self.workflow.connect(self.preinfosource, 'results_labels', self.masking, "inputnode.results_labels")

        self.workflow.connect(self.results_label_node, self.results_label_file, self.masking, "inputnode.results_label_img")
        self.workflow.connect(self.init_pet, 'outputnode.pet_volume', self.masking, "inputnode.pet_volume")

        # If <pvc/quant/results>_label_template has been set, this means that label_img[0] contains the file path
        # to stereotaxic atlas and label_template contains the file path to the template image for the atlas
        if not opts.pvc_label_template == None and opts.pvc_method != None: 
            self.workflow.connect(self.datasource, "pvc_label_template", self.masking, "inputnode.pvc_label_template")
        if not opts.quant_label_template == None and opts.quant_method != None: 
            self.workflow.connect(self.datasource, "quant_label_template", self.masking, "inputnode.quant_label_template")
        if not opts.results_label_template == None: 
            self.workflow.connect(self.datasource, "results_label_template", self.masking, "inputnode.results_label_template")



        #
        # Transform Surfaces 
        #
        if opts.use_surfaces:
            self.workflow.connect(self.datasourceSurf, 'surf_left', self.masking, 'inputnode.surf_left')
            self.workflow.connect(self.datasourceSurf, 'surf_right', self.masking, 'inputnode.surf_right')

    #############################
    # Partial-volume correction #
    #############################
    def set_pvc(self, opts) :
        self.pvc = pvc.get_pvc_workflow("pvc", self.infosource, opts) 
        self.workflow.connect(self.pet_input_node, self.pet_input_file, self.pvc, "inputnode.in_file") 
        self.workflow.connect(self.masking, "pvcLabels.out_file", self.pvc, "inputnode.mask_file") 
        self.workflow.connect(self.init_pet, 'outputnode.pet_header_json', self.pvc, "inputnode.header") 
        #Add the outputs of PVC to list that keeps track of the outputnodes, images, and the number 
        #of dimensions of these images
        self.out_node_list += [self.pvc]
        self.out_img_list += ['outputnode.out_file']
        self.out_img_dim += ['4']
        self.extract_values += [True]
        self.datasink_dir_name += ['pvc']
        print('set pvc')

    ###########################
    # Tracer kinetic analysis #
    ###########################
    def set_quant (self, opts) :
        if opts.pvc_method != None : 
            self.quant_target_wf = self.pvc
            self.quant_target_img='outputnode.out_file'
        else : 
            self.quant_target_wf = self.pet_input_node # #CHANGE
            self.quant_target_img= self.pet_input_file # ##CHANGE
        self.quant=quant.get_quant_workflow("quantification", opts)
        self.workflow.connect(self.init_pet, 'outputnode.pet_header_json', self.quant, "inputnode.header")
        self.workflow.connect(self.masking, "resultsLabels.out_file", self.quant, "inputnode.mask") 
        self.workflow.connect(self.quant_target_wf, self.quant_target_img, self.quant, "inputnode.in_file")
        self.workflow.connect(self.mri_preprocess, self.brain_mask_space_stx_name, self.quant, "inputnode.stereo")
       

        if opts.analysis_space in ["t1", "pet"] :
            if opts.pet_coregistration_target == 't1' :
                self.workflow.connect(self.mri_preprocess, 'outputnode.tfm_mri_stx', self.quant, 'inputnode.tfm_mri_stx')
            else :
                self.workflow.connect(self.pet2mri, "out_matrix", self.quant, 'inputnode.tfm_mri_stx')

            if opts.analysis_space == "pet" :
                self.workflow.connect(self.pet2mri, "out_matrix", self.quant, 'inputnode.tfm_pet_mri')
        
        if opts.arterial :
            self.workflow.connect(self.datasource, 'arterial_file', self.quant, "inputnode.reference")
        else :     
            self.workflow.connect(self.masking, 'quantLabels.out_file', self.quant, "inputnode.reference")

        #Add the outputs of TKA (Quuantification) to list that keeps track of the outputnodes, images, 
        # and the number of dimensions of these images       
        self.out_node_list += [self.quant]
        self.out_img_list += ['outputnode.out_file']
        self.out_img_dim += ['3']
        self.extract_values += [True]
        self.datasink_dir_name += ['quant']

        if opts.quant_to_stereo and not opts.analysis_space == "stereo" :
            self.out_node_list += [self.quant]
            self.out_img_list += ['outputnode.out_file']
            self.out_img_dim += ['3']
            self.extract_values += [False]
            self.datasink_dir_name += ['quant/stereo']
        print('set quant')
        

    ##################
    # Results Report #
    ##################
    # For each of the nodes in the outputnode list pass the output image to mincgroupstats.
    # This will print out descriptive statistics for the labelled regions in the mask image
    # for the output image. 
    def set_results_report(self,  opts ):
        self.results_report(opts)

    def set_results_report_surf(self, opts ):
        self.results_report(opts, surf='surf') 

    def results_report(self, opts, surf='') :
        surf_dir=''
        if surf != '' :
            surf_dir=surf+'_'

        for node, img, dim, extract in zip(self.out_node_list, self.out_img_list, self.out_img_dim, self.extract_values):
            print(node.name, extract, img, dim)
            if not extract : continue
            node_name="results_"+surf+ node.name
            dir_name = "results_"+surf_dir+ node.name
            
            if opts.pvc_label_name != None :
                node_name += "_"+opts.pvc_label_name
            if opts.quant_label_name != None :
                node_name += "_"+opts.quant_label_name
            if opts.results_label_name != None :
                node_name += "_"+opts.results_label_name

            self.resultsReport = pe.Node(interface=results.resultsCommand(), name=node_name)
            self.resultsReport.inputs.dim = dim
            self.resultsReport.inputs.node = node.name
            self.resultsReport.inputs.acq = opts.acq
            self.workflow.connect(self.infosource, 'sid', self.resultsReport, "sub")
            self.workflow.connect(self.infosource, 'ses', self.resultsReport, "ses")
            self.workflow.connect(self.infosource, 'task', self.resultsReport, "task")
            self.workflow.connect(self.infosource, 'run', self.resultsReport, "run")
            self.workflow.connect(self.init_pet, 'outputnode.pet_header_json', self.resultsReport, "pet_header_json")
            self.workflow.connect(node, img, self.resultsReport, 'in_file')
            if opts.use_surfaces :
                self.workflow.connect(self.masking, 'surface_left_node.out_file', self.resultsReport, "surf_left")
                self.workflow.connect(self.datasourceSurf, 'mask_left', self.resultsReport, 'mask_left')
                self.workflow.connect(self.masking, 'surface_right_node.out_file', self.resultsReport, "surf_right")
                self.workflow.connect(self.datasourceSurf, 'mask_right', self.resultsReport, 'mask_right')   
            else :
                self.workflow.connect(self.masking, 'resultsLabels.out_file', self.resultsReport, 'mask')

            self.workflow.connect( self.resultsReport, 'out_file', self.datasink, node_name+os.sep+dir_name )


    ############################
    # Subject-level QC Metrics #
    ############################
    def set_qc_metrics(self, opts):
        if opts.group_qc or opts.test_group_qc :
            #Automated QC: PET to MRI linear coregistration 
            self.distance_metricNode=pe.Node(interface=qc.coreg_qc_metricsCommand(),name="coreg_qc_metrics")
            self.workflow.connect(self.pet2mri, 'outputnode.petmri_img',  self.distance_metricNode, 'pet')
            self.workflow.connect(self.mri_preprocess, self.brain_mask_space_mri_name, self.distance_metricNode, 'brain_mask_space_mri')
            self.workflow.connect(self.mri_preprocess, self.mri_space_nat_name,  self.distance_metricNode, 't1')
            self.workflow.connect(self.infosource, 'ses', self.distance_metricNode, 'ses')
            self.workflow.connect(self.infosource, 'task', self.distance_metricNode, 'task')
            self.workflow.connect(self.infosource, 'sid', self.distance_metricNode, 'sid')

            if  opts.pvc_method != None :
                #Automated QC: PVC 
                self.pvc_qc_metricsNode=pe.Node(interface=qc.pvc_qc_metrics(),name="pvc_qc_metrics")
                self.pvc_qc_metricsNode.inputs.fwhm = list(opts.scanner_fwhm)
                self.workflow.connect(self.pet_input_node, self.pet_input_file, self.pvc_qc_metricsNode, 'pve') 
                self.workflow.connect(self.pvc, "outputnode.out_file", self.pvc_qc_metricsNode, 'pvc'  )
                self.workflow.connect(self.infosource, 'sid', self.pvc_qc_metricsNode, "sub")
                self.workflow.connect(self.infosource, 'ses', self.pvc_qc_metricsNode, "ses")
                self.workflow.connect(self.infosource, 'task', self.pvc_qc_metricsNode, "task")

    #############
    # Dashboard # 
    #############
    def set_dashboard(self, opts) : 
        if opts.analysis_space == "pet":
            self.t1_analysis_space=pe.Node(niu.IdentityInterface(fields=["output_image"]),name="t1_analysis_space")
            self.workflow.connect(self.pet2mri, "inverse_warped_image", self.t1_analysis_space,"output_image")
        elif opts.analysis_space == "t1":
            self.t1_analysis_space=pe.Node(niu.IdentityInterface(fields=["output_image"]),name="t1_analysis_space")
            self.workflow.connect(self.mri_preprocess, self.mri_space_nat_name, self.t1_analysis_space,"output_image")
        elif opts.analysis_space == "stereo":
            self.t1_analysis_space=pe.Node(niu.IdentityInterface(fields=["output_image"]),name="t1_analysis_space")
            self.workflow.connect(self.mri_preprocess, "outputnode.mri_space_stx", self.t1_analysis_space,"output_image")
        
        self.dashboard=pe.Node(interface=dash.deployDashCommand(),name="dash_scanLevel")
        self.dashboard.inputs.targetDir = opts.targetDir;
        self.dashboard.inputs.sourceDir = opts.sourceDir;
        self.dashboard.inputs.analysis_space = opts.analysis_space
        self.workflow.connect(self.infosource, 'sid', self.dashboard, "sid")
        self.workflow.connect(self.infosource, 'ses', self.dashboard, "ses")
        self.workflow.connect(self.infosource, 'task', self.dashboard, "task")
        self.workflow.connect(self.infosource, 'run', self.dashboard, "run")
        self.workflow.connect(self.infosource, 'cid', self.dashboard, "cid")
        self.workflow.connect(self.pet2mri, 'warped_image',  self.dashboard, 'pet_space_mri')
        self.workflow.connect(self.t1_analysis_space, 'output_image',  self.dashboard, 't1_analysis_space')
        self.workflow.connect(self.mri_preprocess, self.mri_space_nat_name , self.dashboard,"mri_space_nat")
        self.workflow.connect(self.pet_input_node, self.pet_input_file, self.dashboard, "pet")
        if opts.pvc_method != None :
            self.dashboard.inputs.pvc_method = opts.pvc_method;
            self.workflow.connect(self.pvc, 'outputnode.out_file',  self.dashboard, 'pvc')
        if opts.quant_method != None:
            self.dashboard.inputs.quant_method = opts.quant_method;
            self.workflow.connect(self.quant, 'outputnode.out_file',  self.dashboard, 'quant')
    #####################
    ### Preinfosource ###
    #####################
    def set_preinfosource(self, opts):
        self.preinfosource = pe.Node(interface=niu.IdentityInterface(fields=['args','ses','results_labels','quant_labels','pvc_labels', 'pvc_erode_times', 'quant_erode_times', 'results_erode_times']), name="preinfosource")
        self.preinfosource.iterables = ( 'args', opts.task_valid_args )
        self.preinfosource.inputs.results_labels = opts.results_labels
        self.preinfosource.inputs.quant_labels = opts.quant_labels
        self.preinfosource.inputs.pvc_labels = opts.pvc_labels 
        self.preinfosource.inputs.results_erode_times = opts.results_erode_times
        self.preinfosource.inputs.quant_erode_times = opts.quant_erode_times
        self.preinfosource.inputs.pvc_erode_times = opts.pvc_erode_times

    ##################
    ### Infosource ###
    ##################
    def set_infosource(self, opts):
        self.infosource = pe.Node(interface=init.SplitArgsRunning(), name="infosource")
        self.workflow.connect(self.preinfosource, 'args', self.infosource, "args")

    ####################
    # Base Datasources #
    ####################
    def set_datasource_base(self, opts):
        '''
        <datasource> is just an identity interface that doesn't actually do anything. Files from nodes from 
        datasourcePET and datasourceAnat are linked to it. This makes it easier to refer to an input file without
        having to remember if it came from datasourcePET or datasourceAnat. Also makes it possible to eventually 
        have a version of APPIAN that only uses PET input.
        '''
        self.datasource = pe.Node(niu.IdentityInterface(fields=self.base_anat_outputs+self.base_pet_outputs), name="datasource") 
        # connect PET datasource files
        self.workflow.connect(self.datasourcePET, 'json_header',self.datasource, 'json_header' )
        self.workflow.connect(self.datasourcePET, 'pet',self.datasource, 'pet' )
        if opts.arterial :
            self.workflow.connect(self.datasourcePET, 'arterial_file', self.datasource, 'arterial_file')

        # connect datasourceAnat files
        if opts.user_mri_stx != '' :
            self.workflow.connect(self.datasourceAnat, 'tfm_mri_stx',self.datasource, 'tfm_mri_stx' )
            self.workflow.connect(self.datasourceAnat, 'tfm_stx_mri',self.datasource, 'tfm_stx_mri' )
        if opts.user_brainmask :
            self.workflow.connect(self.datasourceAnat, 'brain_mask_space_stx',self.datasource, 'brain_mask_space_stx' )
        self.workflow.connect(self.datasourceAnat, 'mri',self.datasource, 'mri' )

        if opts.pvc_method != None and opts.pvc_label_type != "internal_cls"  :
            self.workflow.connect(self.datasourceAnat, 'pvc_label_img', self.datasource, 'pvc_label_img')
        
        if opts.quant_method != None and opts.quant_label_type != "internal_cls" :
            self.workflow.connect(self.datasourceAnat, 'quant_label_img', self.datasource, 'quant_label_img')
        
        if opts.results_label_type != "internal_cls" :
            self.workflow.connect(self.datasourceAnat, 'results_label_img', self.datasource, 'results_label_img')

        if opts.pvc_label_template != None :
            self.workflow.connect(self.datasourceAnat, 'pvc_label_template', self.datasource, 'pvc_label_template')

        if opts.quant_label_template != None :
            self.workflow.connect(self.datasourceAnat, 'quant_label_template', self.datasource, 'quant_label_template')

        if opts.results_label_template != None :
            self.workflow.connect(self.datasourceAnat, 'results_label_template', self.datasource, 'results_label_template')

    ##################
    # PET Datasource #
    ##################
    def set_datasource_pet(self, opts ):
        self.base_pet_outputs = [ 'pet', "json_header", "arterial_file" ]
        self.datasourcePET = pe.Node( interface=nio.DataGrabber(infields=[], outfields=self.base_pet_outputs, raise_on_empty=True, sort_filelist=False), name="datasourcePET")
        self.datasourcePET.inputs.template = '*'
        self.datasourcePET.inputs.base_directory = '/' # opts.sourceDir
        self.datasourcePET.inputs.acq=opts.acq
        self.datasourcePET.inputs.rec=opts.rec  
        self.datasourcePET.inputs.field_template = {}
        self.datasourcePET.inputs.template_args = {}

        pet_str = opts.sourceDir+os.sep+'sub-%s/pet/sub-%s' 
        pet_list = ['sid', 'sid' ]
        if len(opts.sessionList) != 0: 
            pet_str = pet_str + '*ses-%s'
            pet_str=re.sub('/pet/','/*ses-%s/pet/',pet_str)
            pet_list.insert(1, 'ses')
            pet_list += ['ses'] 
        if len(opts.taskList) != 0: 
            pet_str = pet_str + '*task-%s'
            pet_list += ['task'] 
        if len(opts.runList) != 0: 
            pet_str = pet_str + '*run-%s'
            pet_list += ['run']
        if opts.acq != '' :
            pet_str = pet_str + '*acq-%s'
            pet_list += ['acq']  
        if opts.rec != '':
            pet_str = pet_str + '*rec-%s'
            pet_list += ['rec']


        pet_str = pet_str + '*_pet.'

        img_str = pet_str + opts.img_ext + '*'
        header_str = pet_str + 'json'
        field_template_pet = dict( pet=img_str, json_header=header_str )
        template_args_pet =  dict( pet=[pet_list], json_header=[pet_list] )

        if opts.arterial : 
            field_template_pet["arterial_file"] = pet_str + 'dft' 
            template_args_pet["arterial_file"] = [pet_list]

        self.datasourcePET.inputs.field_template.update(field_template_pet)
        self.datasourcePET.inputs.template_args.update(template_args_pet)

        #Create connections bettween infosource and datasourcePET 
        self.workflow.connect([
            (self.infosource,self.datasourcePET, [('sid', 'sid')]),
            (self.infosource,self.datasourcePET, [('ses', 'ses')]),
            (self.infosource,self.datasourcePET, [('cid', 'cid')]),
            (self.infosource,self.datasourcePET, [('task', 'task')]),
            (self.infosource,self.datasourcePET, [('run', 'run')]),
            ])


    ###################
    # Anat Datasource #
    ###################
    def set_datasource_anat(self, opts) :  
        ### Use DataGrabber to get key input files
        self.base_anat_outputs  = ['mri', 'tfm_mri_stx','tfm_stx_mri','brain_mask_space_stx', "pvc_label_img", "quant_label_img", "results_label_img", "pvc_label_template", "quant_label_template", "results_label_template" ]
        self.datasourceAnat = pe.Node( interface=nio.DataGrabber(infields=[], outfields=self.base_anat_outputs, raise_on_empty=True, sort_filelist=False), name="datasourceAnat")
        self.datasourceAnat.inputs.template = '*'
        self.datasourceAnat.inputs.base_directory = '/' # opts.sourceDir

        base_label_template=opts.sourceDir+os.sep+'sub-%s/DIR/sub-%s'
        base_template_args=['sid','sid']

        if len(opts.sessionList) != 0: 
            base_label_template = re.sub('DIR', '*ses-%s/DIR/', base_label_template)
            base_label_template +=  '*ses-%s' 
            base_template_args.insert(1, 'ses')
            base_template_args += ['ses'] 
        
        mri_list = base_template_args
        mri_str = re.sub('DIR', 'anat', base_label_template)

        self.datasourceAnat.inputs.field_template={
                "mri":mri_str+"*_T1w.nii*"
                #opts.sourceDir+os.sep+'sub-%s/*ses-%s/anat/sub-%s_ses-%s*_T1w.'+opts.img_ext+'*'
                }
        self.datasourceAnat.inputs.template_args = {"mri":[mri_list]}

        if opts.pvc_label_type != "internal_cls" :
            self.set_label(opts.pvc_label_type ,opts.pvc_label_img,opts.pvc_label_template, 'pvc_label_img', 'pvc_label_template', opts, base_label_template, base_template_args)

        if opts.quant_label_type != "internal_cls" :
            self.set_label(opts.quant_label_type , opts.quant_label_img, opts.quant_label_template, 'quant_label_img', 'quant_label_template', opts, base_label_template, base_template_args)

        if opts.results_label_type != "internal_cls" :
            self.set_label(opts.results_label_type , opts.results_label_img, opts.results_label_template, 'results_label_img', 'results_label_template', opts, base_label_template, base_template_args)

        if opts.user_mri_stx != '' :
            tfm_label_template=re.sub('DIR', 'transforms', base_label_template )
            self.set_transform(opts, tfm_label_template, base_template_args)

        if opts.user_brainmask :
            self.set_brain_mask(opts, mri_str, base_template_args)
        
        #Create connections bettween infosource and datasourceAnat
        self.workflow.connect([
            (self.infosource,self.datasourceAnat, [('sid', 'sid')]),
            (self.infosource,self.datasourceAnat, [('ses', 'ses')]),
            ])
    #
    # Set Labels for datasourceAnat
    #
    def set_label(self, label_type, img, template, label_img, template_img, opts, base_label_template, base_template_args) :
        '''
        updates datasourceT1 with the appropriate field_template and template_args to find the desired
        3D image volume with labels for particular processing stage (pvc, quant, results)
        '''
        field_template={}
        template_args={}
        if label_type == 'user_cls' :
            template_args[label_img]=[ base_template_args + [img] ] 
            field_template[label_img] = base_label_template + '*%s*'
        elif label_type == 'atlas' or label_type == 'atlas-template' :
            field_template[label_img] = "%s"
            template_args[label_img] = [[img]]       
            if label_type == 'atlas-template'  :
                field_template[template_img] = "%s"
                template_args[template_img] = [[template]]
        else :
            print("Error : label_type not valid", label_type)
            exit(1)
        self.datasourceAnat.inputs.field_template.update( field_template )
        self.datasourceAnat.inputs.template_args.update( template_args )
    
    #
    # Set Brain Mask for datasourceAnat
    #
    def set_brain_mask(self, opts, base_label_template, base_template_args) :
        field_template={}
        template_args={}

        brain_mask_template = opts.sourceDir+os.sep+'sub-%s/*ses-%s/anat/sub-%s_ses-%s*'
        template_args["brain_mask_space_stx"]=[['sid' ,'ses','sid', 'ses']]

        brain_mask_template = brain_mask_template + "_T1w_space-mni"

        if not opts.coregistration_brain_mask : 
            brain_mask_template = brain_mask_template + '_skullmask.*'+opts.img_ext
        else :
            brain_mask_template = brain_mask_template + '_brainmask.*'+opts.img_ext

        field_template["brain_mask_space_stx"] = brain_mask_template
        self.datasourceAnat.inputs.field_template.update(field_template)
        self.datasourceAnat.inputs.template_args.update(template_args)

    #
    # Set transformation files for datasourceAnat
    #
    def set_transform(self, opts, base_label_template, base_template_args):
        field_template={}
        template_args={}
        label_template = base_label_template
        template_args["tfm_mri_stx"] = [base_template_args]
        template_args["tfm_stx_mri"] = [base_template_args]
        
        mri_stx_template = label_template + '*to-MNI152*mode-image_xfm.h5' 
        stx_mri_template = label_template + '*to-T1w*mode-image_xfm.h5' 
        #if opts.user_mri_stx == 'nl' :
        #    label_template = label_template + '*_xfm.h5' 
        #elif opts.user_mri_stx == 'lin' : 
        #    label_template = label_template + '*_xfm.h5' 
        #else :
        #    print("Error : Options to '--user-t1mni' must either be 'lin' or 'nl'")
        #    exit(1)
        field_template["tfm_mri_stx"] = mri_stx_template
        field_template["tfm_stx_mri"] = stx_mri_template

        self.datasourceAnat.inputs.field_template.update(field_template)
        self.datasourceAnat.inputs.template_args.update(template_args)

    ###########################
    # Datasource for Surfaces #
    ###########################
    def set_datasource_surf(self, opts):
        ### Use DataGrabber to get sufraces
        self.datasourceSurf = pe.Node( interface=nio.DataGrabber(infields=['sid', 'ses', 'task', 'acq', 'rec', 'label'], outfields=['surf_left','mask_left', 'surf_right', 'mask_right'], raise_on_empty=True, sort_filelist=False), name="datasourceSurf")
        self.datasourceSurf.inputs.base_directory = opts.sourceDir
        self.datasourceSurf.inputs.template = '*'
        self.datasourceSurf.inputs.acq=opts.acq
        self.datasourceSurf.inputs.rec=opts.rec
        self.datasourceSurf.inputs.label=opts.surface_label
        self.datasourceSurf.inputs.field_template =dict(
                surf_left="sub-%s/_ses-%s/anat/sub-%s_ses-%s_*T1w_hemi-L_space-stereo_midthickness.surf.obj",
                surf_right="sub-%s/_ses-%s/anat/sub-%s_ses-%s_*T1w_hemi-R_space-stereo_midthickness.surf.obj",
                #FIXME Not sure what BIDS spec is for a surface mask
                mask_left="sub-%s/_ses-%s/anat/sub-%s_ses-%s_*T1w_hemi-L_space-stereo_%s.txt",
                mask_right="sub-%s/_ses-%s/anat/sub-%s_ses-%s_*T1w_hemi-R_space-stereo_%s.txt",
                )
        self.datasourceSurf.inputs.template_args = dict(
                surf_left = [['sid', 'ses', 'sid', 'ses']],
                surf_right = [['sid', 'ses', 'sid', 'ses']],
                mask_left = [['sid', 'ses', 'sid', 'ses', 'label']],
                mask_right = [['sid', 'ses', 'sid', 'ses','label']]
                )
        self.workflow.connect([
            (self.infosource,self.datasourceSurf, [('sid', 'sid')]),
            (self.infosource,self.datasourceSurf, [('cid', 'cid')]),
            (self.infosource,self.datasourceSurf, [('task', 'task')]),
            (self.infosource,self.datasourceSurf, [('ses', 'ses')]),
            (self.infosource,self.datasourceSurf, [('run', 'run')]),
            ])

    ##############
    ###Datasink###
    ##############
    def set_datasink(self, opts) :
        self.datasink=pe.Node(interface=nio.DataSink(), name="output")
        self.datasink.inputs.base_directory= opts.targetDir + '/' 
        self.datasink.inputs.substitutions = [('_args_',''), ('run','run-'), ('_cid_', ''), ('sid_', ''), ('sid-','sub-'), ('task','task-'), ('ses','ses')]
        for i, (node, img, dim, dir_name) in enumerate(zip(self.out_node_list, self.out_img_list, self.out_img_dim, self.datasink_dir_name)):
            if opts.output_format == 'minc' :
                convertOutput=pe.Node(nii2mncCommand(), name="convert_output_"+str(i)+'_'+node.name)
                self.workflow.connect(node, img, convertOutput, 'in_file')
                self.workflow.connect(convertOutput, 'out_file', self.datasink, dir_name) 
            else :
                self.workflow.connect(node, img, self.datasink, dir_name) 
        return 0


