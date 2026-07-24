# ECS-on-Fargate-Monitoring-Observability-with-Datadog

## 1. 개요

### 1.1 배경 및 목적

- Amazon ECS on AWS Fargate는 호스트 OS와 커널에 대한 사용자 접근을 원천적으로 차단하는 서버리스 컴퓨팅 환경이다
- 이 환경에서는 EC2 기반 인프라에서 당연하게 여겨지던 "호스트에 모니터링 에이전트를 설치한다"는 접근 방식이 성립하지 않는다
- 본 프로젝트는 이러한 제약 하에서 인프라, 네트워크, 로그, APM 데이터를 유실 없이 수집하여 Datadog으로 통합하는 방법과 아키텍처를 정리하는 것을 목적으로 한다

### 1.2 범위

- ECS on Fargate 환경의 구조적 특성(awsvpc, ECS Service Connect)과 이로부터 파생되는 관측성 제약
- `AWS FireLens(Fluent Bit)`, `Datadog Agent`,를 통한 인프라·네트워크·로그·APM 데이터 수집 방법
- FastAPI 기반 MSA 데모 환경을 통한 검증

### 1.3 키워드

- ECS
    - 컨테이너화된 애플리케이션의 배포-스케일링을 관리하는 오케스트레이션 서비스
- Task/Task Definition
    - ECS에서 실행되는 컨테이너 그룹의 실행 단위 / 그에 대한 정의 파일
- Fargate
    - 컨테이너에 필요한 컴퓨팅 자원을 태스크 단위로 동적 할당하는 Serverless 컴퓨팅 엔진
- awsvpc
    - ECS Task마다 독립된 ENI와 사설 IP를 부여하는 네트워크 모드
    - Fargate 환경에서 강제되는 네트워크 모드
- ENI
    - AWS의 가상 네트워크 인터페이스
- ECS Service Connect
    - ECS Service간 서비스 디스커버리와 트래픽 관리를 제공하는 Managed 기능
- SideCar Pattern
    - 하나의 Task/Pod 내에 보조 기능을 수행하는 컨테이너를 함께 배치하는 패턴
- Envoy
    - Service Connect 활성화 시 자동 주입되는 오픈소스 프록시
- AWS FireLens
    - ECS Task 내에서 로그 라우팅을 설정할 수 있게 해주는 기능
    - AWS의 독립된 서비스는 아님
- Fluent Bit & Fluentd
    - 사이드카로 실행되는 로그 수집-라우팅 오픈소스 소프트웨어
    - FireLens의 실제 로그 처리 로직 담당
- Datadog
    - Metric, Log, Trace 중앙 플랫폼
    - Datadog Agent, FireLens를 통하여 해당 플랫폼에 관측 데이터 전송
- dd-trace
    - 애플리케이션 코드에 연동해 분산 트레이스를 수집하는 Datadog APM 기능의 SDK

---

## 2. ECS on Fargate 환경 설명

### 2.1 ECS/Fargate 정의

- ECS는 컨테이너화된 애플리케이션의 배포와 스케일링을 관리하는 오케스트레이션 서비스
- 컨테이너를 실행할 컴퓨팅 자원으로 EC2 launch type과 Fargate launch type 중 하나를 선택할 수 있다
- Fargate는 컨테이너에 필요한 vCPU·메모리를 태스크 단위로 동적 할당하는 서버리스 컴퓨팅 엔진이다
- 사용자는 기반 EC2 인스턴스를 프로비저닝하거나 관리하지 않으며, 해당 인스턴스의 OS·커널에 접근할 수 있는 수단(SSH, SSM 세션 등)이 제공되지 않는다
- 이 "호스트/커널 접근 완전 격리"라는 특성이 3장에서 다룰 모든 관측성 제약의 공통 근원이 된다.

### 2.2 네트워킹 구조 (awsvpc)

- `awsvpc`는 ECS Task마다 독립된 ENI와 고유 사설 IP를 할당하는 네트워크 모드다
- `Fargate`에서는 이 모드만 지원되며(`bridge`, `host` 모드는 EC2 launch type 전용), 사실상 강제된다
- 같은 Task 내에 정의된 여러 컨테이너는 이 ENI를 공유하므로 localhost로 서로 통신할 수 있다
- (이는 4장에서 다룰 Datadog Agent 사이드카 패턴, dd-trace의 트레이스 전송이 성립하는 전제 조건이다)

### 2.3 ECS Service Connect

- ECS Service Connect는 ECS Task/Service 간 서비스 디스커버리와 트래픽 관리를 제공하는 AWS 관리형 기능이다
- ECS 환경에서의 `Service Mesh`로 간주할 수 있다
- 활성화하면 `Envoy`기반 프록시 컨테이너가 Task 내에 자동으로 주입되어, 애플리케이션 코드 수정 없이 서비스 간 통신을 중계한다
- `AWS App Mesh`가 서비스 종료 예정이되면서, ECS 환경에서 서비스 메시가 필요한 경우 Service Connect가 사실상 표준 선택지가 되었다
    - (EKS 환경의 istio와 개념적으로 대응되나, 본 프로젝트에서는 비교 언급 수준으로만 다룬다)
    

---

## 3. 관측성 관점의 태생적 제약사항

### 3.1 호스트 접근 불가로 인한 제약

- 호스트/커널 접근 불가로 인한 제약 (Fargate 아키텍처 자체의 근본 제약)
- 2.1에서 설명한 대로 Fargate는 호스트 OS·커널에 대한 사용자 접근을 제공하지 않는다
    - 이 하나의 특성이 세 갈래의 관측성 제약을 만든다
- **인프라 메트릭 수집 제약**
    - 전통적으로 CPU/메모리/디스크 메트릭은 호스트에 설치된 에이전트가 `/proc`, cgroup 등을 직접 읽어 수집한다
    - Fargate는 이 설치 자체가 불가능하다. → 4.1에서 해결
- **로그 수집 제약**
    - 호스트의 로그 파일(`/var/log/...`)에 접근하거나 호스트 단위로 로그 수집 에이전트를 설치하는 방식이 불가능하다. → 4.3에서 해결
- **~~커널 레벨 네트워크 모니터링 제약~~**
    - ~~Datadog Cloud Network Monitoring(CNM)은 eBPF 기반 system-probe로 커널 레벨에서 패킷·커넥션을 관찰하는데, 이 역시 커널 접근을 전제로 한다~~
    - ~~ECS Fargate에서 CNM은 **Preview 단계이며 Datadog 담당자에게 별도 신청해야 활성화 가능**하다 (self-service 불가)~~
    - ~~활성화 시 표준 eBPF 방식이 아닌 eBPF-less 모드로 동작하며, 다음 3개 환경변수와 capability가 모두 필요하다~~
        - ~~Datadog Agent 7.58 이상 필요~~
        
        ```jsx
        {
         "containerDefinitions": [
           (...)
             "environment": [
               (...)
               {
                 "name": "DD_SYSTEM_PROBE_NETWORK_ENABLED",
                 "value": "true"
               },
               {
                  "name": "DD_NETWORK_CONFIG_ENABLE_EBPFLESS",
                  "value": "true"
               },
               {
                  "name": "DD_PROCESS_AGENT_ENABLED",
                  "value": "true"
               }      
             ],
             "linuxParameters": {
              "capabilities": {
                "add": [
                  "SYS_PTRACE"
                ]
              }
            },
         ],
        }
        
        ```
        
    - ~~→ 4.2에서 대안(정확히는 "보완책": CNM 자체의 대체가 아니라 별도 관측 지점 확보) 제시~~

### ~~3.2 Service Connect 프록시 메트릭 미노출 문제~~

- ~~2.3에서 설명한 Envoy 기반 프록시는 Task 내 모든 서비스 간 트래픽을 중계하므로, 이론적으로는 마이크로서비스 간 RPS·에러율·레이턴시·커넥션 상태를 관측할 수 있는 최적의 지점이다~~
- ~~그러나 Service Connect의 Envoy는 완전 관리형(Managed Envoy)이다. App Mesh나 Kubernetes/Istio처럼 사용자가 Envoy bootstrap 설정, 사이드카 이미지, admin 인터페이스 노출 여부를 직접 제어하는 구조가 아니라, AWS가 프록시 배포·설정을 전담하고 사용자에게는 timeout 값 등 극히 제한된 파라미터만 노출한다~~
- ~~이로 인해 표준적인 Envoy 관측 방법(admin/stats 엔드포인트 노출 → Prometheus 스타일 카운터 수집)이 원천적으로 불가능하다~~
    - ~~이건 "기본값이 꺼져 있어서 켜야 하는" 문제가 아니라 "사용자가 켤 수 있는 수단 자체가 없는" 문제다~~
- ~~즉 3.1이 "커널에 접근할 수단이 없다"는 제약이라면, 3.2는 "Envoy 내부에 접근할 수단이 없다"는 같은 층위의 제약이다. 둘 다 관리형 서비스가 특정 레이어를 완전히 추상화하면서 생기는 구조적 공백이라는 공통점이 있다~~
- ~~따라서 해결책은 Envoy 내부를 들여다보는 방식이 아니라, AWS가 자체적으로 큐레이션해서 외부로 노출하는 대체 관측 지점(CloudWatch 네이티브 메트릭, Access Logs)을 활용하는 방식이어야 한다 → 4.2.2에서 해결~~

---

## 4. 관측성 확보 방안

### 4.1 인프라 모니터링

- **배치 구조**
    - Task Definition 내에 Datadog Agent 컨테이너를 사이드카로 추가한다
    - 2.2에서 설명한 awsvpc 네트워크 공유 특성 덕분에 별도 네트워크 설정 없이 같은 Task 내 다른 컨테이너와 통신 가능하다
- **수집 데이터**
    - 개별 컨테이너 단위 CPU/메모리 사용량, 네트워크 I/O(Fargate 플랫폼 버전 1.4.0 이상에서 제공되는 태스크 단위 네트워크 성능 메트릭 포함)
- **필요 설정**
    - `ECS_FARGATE=true` 환경 변수, API Key, 필요 IAM 권한
- 참고
    - 4.1에서 다루는 Datadog Agent 사이드카가 이미 Task 내에 떠 있다는 이유로 "그 Agent가 로그도 같이 수집하면 되지 않나"라고 생각할 수 있으나, 이는 성립하지 않는다
    - Datadog Agent의 표준 로그 수집 방식은 Docker 소켓(`/var/run/docker.sock`)과 호스트의 컨테이너 로그 파일(`/var/lib/docker/containers/...`)을 직접 읽는 방식인데, 이 역시 호스트 접근을 전제로 하므로 Fargate에서는 Agent 자신도 동일한 제약을 받는다
    - 컨테이너의 stdout/stderr는 컨테이너 런타임이 가로채 지정된 로그 드라이버로 전달하는 구조이며, 이 전달 경로에 개입하려면 `awsfirelens` 같은 로그 드라이버로 명시적으로 라우팅해야 한다
    - Datadog Agent는 이 로그 드라이버 역할을 하지 않으므로, 사이드카로 떠 있는 것만으로는 다른 컨테이너의 로그를 수집할 수 없다

### ~~4.2 네트워크 모니터링~~

#### ~~4.2.1 커널 레벨 네트워크 제약 해결 (VPC Flow Logs)~~

- ~~3.1의 커널 레벨 제약(CNM) 대응~~
- ~~CNM은 Preview·신청 필요 → self-service 대안으로 VPC Flow Logs 채택~~
- ~~파이프라인: VPC Flow Logs → Firehose/S3/CloudWatch Logs → (Forwarder Lambda) → Datadog~~
- ~~ECS 메타데이터(Service/Cluster name) 포함 가능, 단 지원 리전 제한 있음~~
- ~~한계: 로그 기반 지연(수 분), DNS 쿼리 레벨 상세 없음, 프로세스 단위 귀속 불가~~

#### ~~4.2.2 Service Connect 프록시 메트릭 수집~~

- ~~원인: Service Connect의 Envoy는 완전 관리형(Managed)이라 사용자가 Envoy 설정에
개입할 수 없음 (3.2 참조)~~
- ~~주 경로: 네이티브 CloudWatch 메트릭 (AWS/ECS 네임스페이스)
- portMappings에 appProtocol(HTTP/HTTP2/GRPC) 지정
- RequestCount, HTTPCode_Target_4XX/5XX_Count, TargetResponseTime,
ActiveConnectionCount, NewConnectionCount
- Datadog: 기존 AWS Integration(ECS 네임스페이스 폴링)으로 수집 예상,
배포 후 Metrics Explorer에서 실제 수집 여부 검증 필수
- 미수집 시 대체: CloudWatch Metric Streams(Kinesis Firehose)~~
- ~~보조 경로: Envoy Access Logs
- serviceConnectConfiguration.accessLogConfiguration, Fargate 1.4.0+
- awsfirelens로 앱 로그와 분리, 4.3 파이프라인 재사용
- 용도: 5.3 Trace-to-Log 시연 시 요청 단위 상세 추적~~
- ~~3.2의 Service Connect Envoy 메트릭 미노출 대응~~
- ~~Service Connect 네이티브 CloudWatch 메트릭 + Envoy Access Logs 병행~~
- **~~수집 지표~~**
    - ~~RPS(`envoy.cluster.upstream_rq_total`)~~
    - ~~에러율(`envoy.cluster.upstream_rq_xx`)~~
    - ~~레이턴시(`envoy.cluster.upstream_rq_time`)~~
    - ~~커넥션 상태(`envoy.cluster.upstream_cx_active`)~~
    

### 4.3 로그 수집

- **Prerequisite**
    - Docker 컨테이너 로그는 기본적으로 stdout/stderr 기반으로 동작하며, Docker 데몬이 이를 가로채 지정된 로그 드라이버로 전달한다
- **AWS FireLens 상세**
    - ECS의 독립된 서비스가 아니라 ECS Task Definition 내에서 사용하는 로그 라우팅 기능이다
    - 실제 로그 처리는 사이드카로 실행되는 Fluent Bit(또는 Fluentd)가 담당하며, `awsfirelens`는 Docker 표준 드라이버가 아니라 ECS가 해석해 `fluentd` 드라이버로 치환하는 pseudo-driver다
- **필요성**
    - Fargate 환경에서는 사이드카 컨테이너가 사실상 유일한 로그 수집 경로이며, FireLens는 이 사이드카 구성(설정 파일 작성, 소켓 연결, 컨테이너 추가)을 Task Definition 내 최소 설정으로 자동화한다
- **Datadog 권장 사항**
    - Datadog 공식 문서는 ECS Fargate 로그 수집 방법으로 (1) FireLens 방식과 (2) `awslogs`+CloudWatch+Lambda Forwarder 방식 두 가지를 제시하며, FireLens를 명시적으로 권장한다
    - 이유는 Fargate Task 내에서 Fluent Bit를 직접 구성할 수 있다는 점, Datadog Fluent Bit 출력 플러그인이 ECS Explorer에서 로그와 ECS 리소스를 연관 짓는 데 쓰이는 추가 태깅을 제공한다는 점이다
- **설정 방법**
    - 로그 라우터 컨테이너에 `firelensConfiguration.type: fluentbit` 지정, 애플리케이션 컨테이너에 `logConfiguration.logDriver: awsfirelens` 및 Datadog 목적지 옵션 지정, Task Role/Execution Role 권한 구성

### 4.4 APM/분산 트레이싱

- 3장에서 제기한 별도의 제약은 없다
- APM은 앱 컨테이너 내부의 dd-trace SDK가 2.2에서 설명한 awsvpc의 localhost 통신을 통해 같은 Task 내 Datadog Agent 사이드카로 트레이스를 전송하는 구조로, 표준 사이드카 패턴 안에서 정상 동작한다
- 본 프로젝트에서 APM을 다루는 이유는 관측성 공백을 메우기 위해서가 아니라, 5장 데모에서 Metric → APM → Log로 이어지는 트러블슈팅 흐름(Trace-to-Log)을 시연하기 위함이다
- **구현 방법**
    - FastAPI 애플리케이션에 dd-trace 라이브러리 연동, 함수 실행 흐름 및 API 요청/응답 트레이스 수집

### 4.5 대시보드 및 알림 설계

- 4.1~4.4에서 수집한 인프라/네트워크/로그/APM 데이터를 통합해 보여주는 Datadog 대시보드 구성
- 이상 상황 감지를 위한 Monitor(알림) 임계치 설계(예: 에러율 급증, 레이턴시 임계치 초과)

---

## 5. 데모 프로젝트

- 데모 프로젝트 명세(DEMO_PR_SPEC) 참조