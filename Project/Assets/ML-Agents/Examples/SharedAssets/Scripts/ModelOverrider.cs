using System;
using System.Collections.Generic;
using UnityEngine;
using Barracuda;
using System.IO;
using MLAgents;

namespace MLAgentsExamples
{
    /// <summary>
    /// Utility class to allow the NNModel file for an agent to be overriden during inference.
    /// This is useful to validate the file after training is done.
    /// The behavior name to override and file path are specified on the commandline, e.g.
    /// player.exe --mlagents-override-model behavior1 /path/to/model1.nn --mlagents-override-model behavior2 /path/to/model2.nn
    /// Note this will only work with example scenes that have 1:1 Agent:Behaviors. More complicated scenes like WallJump
    /// probably won't override correctly.
    /// </summary>
    public class ModelOverrider : MonoBehaviour
    {
        const string k_CommandLineFlag = "--mlagents-override-model";
        // Assets paths to use, with the behavior name as the key.
        Dictionary<string, string> m_BehaviorNameOverrides = new Dictionary<string, string>();

        // Cached loaded NNModels, with the behavior name as the key.
        Dictionary<string, NNModel> m_CachedModels = new Dictionary<string, NNModel>();

        /// <summary>
        /// Get the asset path to use from the commandline arguments.
        /// </summary>
        /// <returns></returns>
        void GetAssetPathFromCommandLine()
        {
            m_BehaviorNameOverrides.Clear();

            var args = Environment.GetCommandLineArgs();
            for (var i = 0; i < args.Length-2; i++)
            {
                if (args[i] == k_CommandLineFlag)
                {
                    var key = args[i + 1].Trim();
                    var value = args[i + 2].Trim();
                    m_BehaviorNameOverrides[key] = value;
                }
            }
        }

        void OnEnable()
        {
            GetAssetPathFromCommandLine();
            if (m_BehaviorNameOverrides.Count > 0)
            {
                OverrideModel();
            }
        }

        NNModel GetModelForBehaviorName(string behaviorName)
        {
            if (m_CachedModels.ContainsKey(behaviorName))
            {
                return m_CachedModels[behaviorName];
            }

            if (!m_BehaviorNameOverrides.ContainsKey(behaviorName))
            {
                Debug.Log($"No override for behaviorName {behaviorName}");
                return null;
            }

            var assetPath = m_BehaviorNameOverrides[behaviorName];

            byte[] model = null;
            try
            {
                model = File.ReadAllBytes(assetPath);
            }
            catch(IOException)
            {
                Debug.Log($"Couldn't load file {assetPath}", this);
                // Cache the null so we don't repeatedly try to load a missing file
                m_CachedModels[behaviorName] = null;
                return null;
            }

            var asset = ScriptableObject.CreateInstance<NNModel>();
            asset.Value = model;
            asset.name = "Override - " + Path.GetFileName(assetPath);
            m_CachedModels[behaviorName] = asset;
            return asset;
        }

        /// <summary>
        /// Load the NNModel file from the specified path, and give it to the attached agent.
        /// </summary>
        void OverrideModel()
        {
            var agent = GetComponent<Agent>();
            agent.LazyInitialize();
            var bp = agent.GetComponent<BehaviorParameters>();

            var nnModel = GetModelForBehaviorName(bp.behaviorName);
            Debug.Log($"Overriding behavior {bp.behaviorName} for agent with model {nnModel?.name}");
            // This might give a null model; that's better because we'll fall back to the Heuristic
            agent.GiveModel($"Override_{bp.behaviorName}", nnModel, InferenceDevice.CPU);

        }
    }
}
