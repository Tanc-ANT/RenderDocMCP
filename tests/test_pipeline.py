"""Pipeline/constant decoding regressions; no RenderDoc, GPU or compiler required."""

from enum import Enum
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class VarType(Enum):
    Float = 0
    Double = 1
    Half = 2
    SInt = 3
    UInt = 4
    SLong = 5
    ULong = 6
    SShort = 7
    UShort = 8
    SByte = 9
    UByte = 10
    Bool = 11
    Struct = 12


rd = types.ModuleType("renderdoc")
rd.VarType = VarType  # Deliberately no VarType.Int (RenderDoc 1.45).
rd.ShaderStage = types.SimpleNamespace(Compute="compute", Pixel="pixel")
rd.ResourceId = types.SimpleNamespace(Null=lambda: 0)
root = Path(__file__).resolve().parents[1] / "renderdoc_extension"
package_name = "pipeline_under_test"
package = types.ModuleType(package_name)
package.__path__ = [str(root)]
services = types.ModuleType(package_name + ".services")
services.__path__ = [str(root / "services")]

# Load the service without the Qt-dependent extension __init__.
with patch.dict(sys.modules, {
    "renderdoc": rd, package_name: package,
    package_name + ".services": services,
}):
    spec = importlib.util.spec_from_file_location(
        package_name + ".services.pipeline_service", root / "services/pipeline_service.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

PipelineService = module.PipelineService
Serializers = module.Serializers
ns = types.SimpleNamespace


def variable(kind, values, field, name="value", rows=1, columns=None, members=()):
    return ns(name=name, type=kind, rows=rows,
              columns=len(values) if columns is None else columns,
              value=ns(**{field: values}), members=members)


def block(**overrides):
    values = dict(name="Params", byteSize=148, fixedBindNumber=7,
                  fixedBindSetOrSpace=2, bufferBacked=True, inlineDataBytes=False)
    values.update(overrides)
    return ns(**values)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.service = PipelineService(ns(IsCaptureLoaded=lambda: True), None)
        self.reflection = ns(constantBlocks=[block()], resourceId=123, entryPoint="main")
        self.descriptor = ns(resource=456, byteOffset=177408, byteSize=4016896)
        self.pipe = ns(
            GetConstantBlock=Mock(return_value=ns(descriptor=self.descriptor)),
            GetComputePipelineObject=Mock(return_value=101),
            GetGraphicsPipelineObject=Mock(return_value=202),
        )
        self.controller = ns(GetCBufferVariableContents=Mock(return_value=[
            variable(VarType.Float, [3.0], "f32v", name="MaxRecombineAbsCocRadius"),
            variable(VarType.SInt, [-1, 4], "s32v"),
        ]))

    def decode(self, stage="compute"):
        return self.service._get_cbuffer_info(
            self.controller, self.pipe, self.reflection, stage,
        )

    def test_modern_descriptor_compute_pso_and_reflection_index(self):
        info = self.decode()[0]
        self.pipe.GetConstantBlock.assert_called_once_with("compute", 0, 0)
        self.controller.GetCBufferVariableContents.assert_called_once_with(
            101, 123, "compute", "main", 0, 456, 177408, 4016896,
        )
        self.assertEqual(info["slot"], 0)  # Reflection index, not b7.
        self.assertEqual(info["binding"], 7)
        self.assertEqual(info["binding_space"], 2)
        self.assertEqual(info["resource_id"], "456")
        self.assertEqual(info["byte_offset"], 177408)
        self.assertEqual(info["size"], 148)
        self.assertEqual(info["variables"][0]["value"], [3.0])
        self.assertEqual(info["variables"][1]["value"], [-1, 4])
        self.assertEqual(info["decode_status"], "decoded")
        self.pipe.GetGraphicsPipelineObject.assert_not_called()

    def test_legacy_bound_cbuffer_and_graphics_pso(self):
        self.pipe = ns(
            GetConstantBuffer=Mock(return_value=ns(
                resourceId=789, byteOffset=64, byteSize=148,
            )), GetGraphicsPipelineObject=lambda: 202,
        )
        info = self.decode("pixel")[0]
        self.pipe.GetConstantBuffer.assert_called_once_with("pixel", 0, 0)
        self.assertEqual(info["decode_status"], "decoded")
        self.controller.GetCBufferVariableContents.assert_called_once_with(
            202, 123, "pixel", "main", 0, 789, 64, 148,
        )

    def test_push_constants_and_inline_data_decode_with_null_resource(self):
        self.descriptor.resource = 0
        for overrides in ({"bufferBacked": False}, {"inlineDataBytes": True}):
            with self.subTest(overrides=overrides):
                self.reflection.constantBlocks = [block(**overrides)]
                self.controller.GetCBufferVariableContents.reset_mock()
                self.assertEqual(self.decode()[0]["decode_status"], "decoded")
                self.assertEqual(self.controller.GetCBufferVariableContents.call_args.args[5], 0)

    def test_unbound_buffer_does_not_fabricate_values(self):
        self.descriptor.resource = 0
        info = self.decode()[0]
        self.assertEqual(info["decode_status"], "unbound")
        self.assertEqual(info["variables"], [])
        self.controller.GetCBufferVariableContents.assert_not_called()

    def test_decoder_error_preserves_binding_and_other_blocks(self):
        self.reflection.constantBlocks.append(block(name="View"))
        self.controller.GetCBufferVariableContents.side_effect = [RuntimeError("decode failed"), []]
        first, second = self.decode()
        self.assertEqual(first["error"], "decode failed")
        self.assertEqual(first["resource_id"], "456")
        self.assertEqual(first["decode_status"], "error")
        self.assertEqual(second["decode_status"], "empty")
        self.assertNotIn("error", second)

    def test_shader_info_uses_compute_pso_for_disassembly(self):
        self.pipe.GetShader = lambda stage: 123
        self.pipe.GetShaderEntryPoint = lambda stage: "main"
        self.pipe.GetShaderReflection = lambda stage: self.reflection
        self.reflection.readOnlyResources = [ns(
            name="SceneColor", textureType="Texture2D", fixedBindNumber=3,
        )]
        self.reflection.readWriteResources = [ns(
            name="Output", textureType="Texture2D", fixedBindNumber=0,
        )]
        self.controller.SetFrameEvent = Mock()
        self.controller.GetPipelineState = lambda: self.pipe
        self.controller.GetDisassemblyTargets = lambda enabled: ["DXIL"]
        self.controller.DisassembleShader = Mock(return_value="disassembly")
        self.service._invoke = lambda callback: callback(self.controller)
        # Parsers needs all stage enum members even when selecting compute.
        with patch.object(module.Parsers, "parse_stage", return_value="compute"):
            info = self.service.get_shader_info(7146, "compute")
        self.controller.SetFrameEvent.assert_called_once_with(7146, True)
        self.controller.DisassembleShader.assert_called_once_with(101, self.reflection, "DXIL")
        self.assertEqual([r["name"] for r in info["resources"]], ["SceneColor", "Output"])
        self.assertEqual(info["resources"][0]["type"], "Texture2D")
        self.assertEqual(info["resources"][1]["access"], "ReadWrite")


class SerializerTests(unittest.TestCase):
    def test_numeric_types_without_old_int_enum(self):
        cases = [
            (VarType.Float, "f32v", [3.0]),
            (VarType.SInt, "s32v", [-2147483648]),
            (VarType.UInt, "u32v", [4294967295]),
            (VarType.Double, "f64v", [1.25]),
            (VarType.Half, "f16v", [0x3c00]),
            (VarType.SLong, "s64v", [-9223372036854775808]),
            (VarType.ULong, "u64v", [18446744073709551615]),
            (VarType.SShort, "s16v", [-32768]),
            (VarType.UShort, "u16v", [65535]),
            (VarType.SByte, "s8v", [-128]),
            (VarType.UByte, "u8v", [255]),
            (VarType.Bool, "u32v", [0, 1]),
        ]
        for kind, field, values in cases:
            with self.subTest(kind=kind):
                result = Serializers.serialize_variables([variable(kind, values, field)])[0]
                expected = [1.0] if kind == VarType.Half else values
                self.assertEqual(result["value"], expected)
                self.assertNotIn("value_error", result)

    def test_legacy_int_enum(self):
        with patch.object(rd, "VarType", ns(Int=99)):
            result = Serializers.serialize_variables([variable(99, [-7], "s32v")])[0]
        self.assertEqual(result["value"], [-7])

    def test_nested_arrays_and_matrix_dimensions(self):
        matrix = variable(VarType.Float, list(range(16)), "f32v", rows=2, columns=3)
        parent = variable(VarType.Struct, [], "u32v", members=[matrix])
        result = Serializers.serialize_variables([parent])[0]
        self.assertNotIn("value", result)
        self.assertEqual(result["members"][0]["value"], list(range(6)))

    def test_nonfinite_values_survive_strict_ipc_json(self):
        values = variable(VarType.Float, [float("nan"), float("inf"), -float("inf")], "f32v")
        result = Serializers.serialize_variables([values])
        self.assertEqual(result[0]["value"], ["NaN", "Infinity", "-Infinity"])
        json.dumps(result, allow_nan=False)

    def test_value_failure_is_visible(self):
        broken = variable(VarType.UInt, [1], "wrong_field")
        info = Serializers.serialize_variables([broken])[0]
        self.assertIn("value_error", info)
        self.assertNotIn("value", info)


if __name__ == "__main__":
    unittest.main()
