# -*- coding: utf-8 -*-
"""
svc_stealth.py - SOTA Anti-Cheat 스텔스 기술 모음
문자열 난독화, 안티디버깅, VM 탐지, 메모리 보호 등
"""

import os
import sys
import ctypes
import platform
import subprocess
import random
import time
from typing import Optional, Callable

# ============================================================
# ① String Obfuscation - 문자열 난독화
# ============================================================
class StringObfuscator:
    """
    런타임 문자열 복호화 래퍼
    정적 분석을 방지하기 위해 문자열을 XOR 암호화하여 저장
    """
    
    def __init__(self, key: Optional[bytes] = None):
        self.key = key if key else os.urandom(32)
    
    def encrypt(self, plaintext: str) -> bytes:
        """문자열을 XOR 암호화"""
        plain_bytes = plaintext.encode('utf-8')
        encrypted = bytearray()
        for i, byte in enumerate(plain_bytes):
            encrypted.append(byte ^ self.key[i % len(self.key)])
        return bytes(encrypted)
    
    def decrypt(self, ciphertext: bytes) -> str:
        """암호화된 바이트를 복호화"""
        decrypted = bytearray()
        for i, byte in enumerate(ciphertext):
            decrypted.append(byte ^ self.key[i % len(self.key)])
        return decrypted.decode('utf-8')
    
    def obfuscated_string(self, plaintext: str) -> Callable[[], str]:
        """
        복호화 함수를 반환하는 클로저
        사용: _s = obfuscator.obfuscated_string("secret_string")
              secret = _s()  # 런타임에 복호화
        """
        encrypted = self.encrypt(plaintext)
        def decryptor():
            return self.decrypt(encrypted)
        return decryptor

# 전역 인스턴스
_obfuscator = StringObfuscator()

def s(plaintext: str) -> Callable[[], str]:
    """문자열 난독화 헬퍼 함수"""
    return _obfuscator.obfuscated_string(plaintext)

# ============================================================
# ② Anti-Debugging - 디버거 탐지
# ============================================================
class AntiDebugger:
    """다양한 디버깅 탐지 기법 구현"""
    
    @staticmethod
    def is_debugger_present() -> bool:
        """Windows API를 통한 디버거 탐지"""
        try:
            # IsDebuggerPresent
            kernel32 = ctypes.windll.kernel32
            if kernel32.IsDebuggerPresent():
                return True
            
            # PEB BeingDebugged 플래그
            peb = ctypes.c_void_p(0)
            # PEB 접근 (x64)
            if sys.maxsize > 2**32:
                # 64비트
                process = kernel32.GetCurrentProcess()
                pbi = ctypes.c_ulonglong()
                ret = kernel32.NtQueryInformationProcess(
                    process, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None
                )
                if ret == 0 and pbi.value != 0:
                    return True
        except Exception:
            pass
        return False
    
    @staticmethod
    def check_remote_debugger() -> bool:
        """원격 디버거 탐지"""
        try:
            kernel32 = ctypes.windll.kernel32
            is_debugged = ctypes.c_bool()
            kernel32.CheckRemoteDebuggerPresent(
                kernel32.GetCurrentProcess(),
                ctypes.byref(is_debugged)
            )
            return is_debugged.value
        except Exception:
            return False
    
    @staticmethod
    def check_debugger_by_timing() -> bool:
        """타이밍 어택으로 디버거 탐지"""
        try:
            # 디버거가 있으면 실행 속도가 느려짐
            start = time.perf_counter()
            for _ in range(100000):
                _ = random.random()
            end = time.perf_counter()
            elapsed = end - start
            # 기준 시간보다 느리면 디버거 의심
            return elapsed > 0.5
        except Exception:
            return False
    
    @staticmethod
    def detect_debugger() -> bool:
        """모든 디버거 탐지 방법 종합"""
        return (AntiDebugger.is_debugger_present() or
                AntiDebugger.check_remote_debugger() or
                AntiDebugger.check_debugger_by_timing())

# ============================================================
# ③ VM Detection - 가상머신 탐지
# ============================================================
class VMDetector:
    """가상머신 환경 탐지"""
    
    @staticmethod
    def check_cpu_id() -> bool:
        """CPUID 명령어로 가상 CPU 탐지"""
        try:
            # WMI 쿼리로 CPU 정보 확인
            result = subprocess.run(
                ['wmic', 'cpu', 'get', 'name'],
                capture_output=True,
                text=True,
                timeout=5
            )
            output = result.stdout.lower()
            vm_keywords = ['vmware', 'virtualbox', 'qemu', 'xen', 'kvm', 'hyper-v']
            return any(keyword in output for keyword in vm_keywords)
        except Exception:
            return False
    
    @staticmethod
    def check_mac_address() -> bool:
        """MAC 주소 패턴으로 VM 탐지"""
        try:
            result = subprocess.run(
                ['getmac'],
                capture_output=True,
                text=True,
                timeout=5
            )
            output = result.stdout.lower()
            # VMware, VirtualBox 특정 MAC 주소 패턴
            vm_mac_patterns = ['00:05:69', '00:0c:29', '00:50:56', '08:00:27']
            return any(pattern in output for pattern in vm_mac_patterns)
        except Exception:
            return False
    
    @staticmethod
    def check_registry_keys() -> bool:
        """레지스트리 키로 VM 탐지"""
        try:
            vm_registry_keys = [
                r'HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\Disk\Enum',
                r'HKEY_LOCAL_MACHINE\HARDWARE\DESCRIPTION\System',
            ]
            for key in vm_registry_keys:
                try:
                    result = subprocess.run(
                        ['reg', 'query', key],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    output = result.stdout.lower()
                    if 'vmware' in output or 'virtualbox' in output:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False
    
    @staticmethod
    def check_system_info() -> bool:
        """시스템 정보로 VM 탐지"""
        try:
            # 메모리/디스크 크기 이상 징후
            import psutil
            mem = psutil.virtual_memory()
            # 너무 작은 메모리 (일반적 VM 설정)
            if mem.total < 2 * 1024**3:  # 2GB 미만
                return True
            return False
        except Exception:
            return False
    
    @staticmethod
    def detect_vm() -> bool:
        """모든 VM 탐지 방법 종합"""
        return (VMDetector.check_cpu_id() or
                VMDetector.check_mac_address() or
                VMDetector.check_registry_keys() or
                VMDetector.check_system_info())

# ============================================================
# ④ Memory Protection - 메모리 보호
# ============================================================
class MemoryProtector:
    """메모리 섹션 보호 및 무결성 체크"""
    
    def __init__(self):
        self.memory_hashes = {}
    
    def protect_string(self, key: str, value: str):
        """문자열을 메모리 해시로 보호"""
        import hashlib
        hash_val = hashlib.sha256(value.encode()).hexdigest()
        self.memory_hashes[key] = hash_val
    
    def verify_string(self, key: str, value: str) -> bool:
        """문자열 무결성 검증"""
        import hashlib
        current_hash = hashlib.sha256(value.encode()).hexdigest()
        return self.memory_hashes.get(key) == current_hash
    
    def get_memory_hash(self, data: bytes) -> str:
        """데이터의 해시 계산"""
        import hashlib
        return hashlib.sha256(data).hexdigest()

# ============================================================
# ⑤ Behavior Analysis 강화 - 인간화된 동작
# ============================================================
class HumanBehaviorSimulator:
    """인간 행동 패턴 시뮬레이터"""
    
    @staticmethod
    def bezier_curve(p0: tuple, p1: tuple, p2: tuple, p3: tuple, t: float) -> tuple:
        """
        3차 베지어 곡선 계산 (자연스러운 마우스 이동)
        p0: 시작점, p3: 끝점
        p1, p2: 제어점
        """
        x = (1-t)**3 * p0[0] + 3*(1-t)**2 * t * p1[0] + 3*(1-t) * t**2 * p2[0] + t**3 * p3[0]
        y = (1-t)**3 * p0[1] + 3*(1-t)**2 * t * p1[1] + 3*(1-t) * t**2 * p2[1] + t**3 * p3[1]
        return (x, y)
    
    @staticmethod
    def generate_mouse_path(start: tuple, end: tuple, num_points: int = 10) -> list:
        """
        자연스러운 마우스 이동 경로 생성
        """
        # 제어점 무작위 생성 (자연스러운 곡선)
        p1 = (start[0] + random.uniform(-50, 50), start[1] + random.uniform(-50, 50))
        p2 = (end[0] + random.uniform(-50, 50), end[1] + random.uniform(-50, 50))
        
        path = []
        for i in range(num_points + 1):
            t = i / num_points
            point = HumanBehaviorSimulator.bezier_curve(start, p1, p2, end, t)
            path.append(point)
        return path
    
    @staticmethod
    def simulate_typo(text: str) -> str:
        """
        오타 시뮬레이션 (5% 확률)
        """
        if random.random() < 0.05 and len(text) > 2:
            idx = random.randint(0, len(text) - 1)
            # 인접 키로 오타 시뮬레이션
            typo_map = {
                'a': 's', 's': 'a', 'd': 'f', 'f': 'd',
                'z': 'x', 'x': 'z', 'c': 'v', 'v': 'c'
            }
            if text[idx] in typo_map:
                text = text[:idx] + typo_map[text[idx]] + text[idx+1:]
        return text
    
    @staticmethod
    def simulate_pause(action_type: str) -> float:
        """
        동작 간 휴식 시간 시뮬레이션
        """
        base_delays = {
            'click': 0.1,
            'type': 0.15,
            'move': 0.05,
            'spell': 0.3
        }
        base = base_delays.get(action_type, 0.1)
        # 가우시안 분포로 자연스러운 변화 (음수 방지를 위해 max 적용)
        val = random.gauss(base, base * 0.3)
        return max(0.001, val)
    
    @staticmethod
    def simulate_afk(duration: float = 30.0):
        """
        AFK 시뮬레이션 (일정 시간 비활동)
        """
        time.sleep(duration)

# ============================================================
# ⑥ Process Integrity Protection
# ============================================================
class ProcessProtector:
    """프로세스 무결성 보호"""
    
    @staticmethod
    def check_process_integrity() -> bool:
        """자기 프로세스 무결성 체크"""
        try:
            import psutil
            current_process = psutil.Process()
            # 의심스러운 부모 프로세스 체크
            parent = current_process.parent()
            if parent:
                parent_name = parent.name().lower()
                suspicious = ['debugger', 'cheat', 'hack', 'injector']
                return not any(s in parent_name for s in suspicious)
            return True
        except Exception:
            return True
    
    @staticmethod
    def check_external_modules() -> bool:
        """외부 DLL 인젝션 탐지"""
        try:
            import psutil
            current_process = psutil.Process()
            loaded_dlls = [dll.lower() for dll in current_process.memory_maps()]
            suspicious_dlls = ['inject', 'hook', 'cheat', 'hack']
            for dll in loaded_dlls:
                if any(s in dll for s in suspicious_dlls):
                    return False
            return True
        except Exception:
            return True

# ============================================================
# 통합 스텔스 체커
# ============================================================
class StealthChecker:
    """모든 스텔스 기능을 통합 관리"""
    
    @staticmethod
    def run_all_checks() -> dict:
        """모든 탐지 기능 실행"""
        results = {
            'debugger_detected': AntiDebugger.detect_debugger(),
            'vm_detected': VMDetector.detect_vm(),
            'process_integrity': ProcessProtector.check_process_integrity(),
            'external_modules': ProcessProtector.check_external_modules()
        }
        return results
    
    @staticmethod
    def is_safe_environment() -> bool:
        """안전한 환경인지 확인"""
        results = StealthChecker.run_all_checks()
        return (not results['debugger_detected'] and
                not results['vm_detected'] and
                results['process_integrity'] and
                results['external_modules'])

# ============================================================
# 초기화 및 테스트
# ============================================================
if __name__ == "__main__":
    print("[Stealth] 스텔스 기능 테스트")
    
    # String Obfuscation 테스트
    test_str = "test_string"
    obfuscated = s(test_str)
    print(f"[Obfuscation] 원본: {test_str}, 복호화: {obfuscated()}")
    
    # Anti-Debugging 테스트
    print(f"[Anti-Debug] 디버거 탐지: {AntiDebugger.detect_debugger()}")
    
    # VM Detection 테스트
    print(f"[VM Detection] VM 탐지: {VMDetector.detect_vm()}")
    
    # 전체 환경 체크
    print(f"[Stealth] 안전한 환경: {StealthChecker.is_safe_environment()}")
